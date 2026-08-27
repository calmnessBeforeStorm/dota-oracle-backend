"""Parsed matches -> `match_snapshots` (spec section 5.1, phase 3).

One row per (match_id, minute), carrying the feature vector and the label. This is the core
of the live dataset, and two properties of it are not negotiable:

  - **No information from the future.** Every value is computed from what was knowable at
    that minute. The building state is replayed from the objectives log rather than read
    off `tower_status_*`, which describes the end of the match (spec section 12).
  - **Splits by `match_id`, never by row.** Forty rows of one match are forty views of the
    same game; splitting them across train and test leaks the outcome (spec section 5.1).
    Nothing here can enforce that, but the table is shaped so the grouping key is present.

Reads only from `raw_matches` and the normalized tables, never from the network, so it can
be re-run whenever the feature set changes.
"""

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.db.models.enums import SeriesFormat
from app.db.models.matches import Match, Series
from app.db.models.raw import RawMatch
from app.db.models.training import MatchSnapshot
from app.features.adapters.opendota import is_parsed, iter_snapshots
from app.features.game_state import SeriesContext
from app.features.live import build_live_features
from app.ingestion.sources import RawSource

log = get_logger(__name__)

#: Spec section 5.3: forfeits and disconnect-ridden games are filtered by metadata, never by
#: outcome. Twelve minutes is the stated floor.
MIN_DURATION_SECONDS = 12 * 60


@dataclass
class FeaturizeReport:
    matches_seen: int = 0
    matches_used: int = 0
    snapshots: int = 0
    skipped: dict[str, int] = field(default_factory=dict)

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1

    def as_log_fields(self) -> dict[str, Any]:
        return {
            "matches_seen": self.matches_seen,
            "matches_used": self.matches_used,
            "snapshots": self.snapshots,
            "skipped": self.skipped,
        }


@dataclass(frozen=True)
class MatchContext:
    """Series-level facts about a map, from our own normalized tables."""

    series_format: SeriesFormat | None = None
    game_in_series: int = 1
    is_conditional_game: bool | None = None
    radiant_series_wins: int = 0
    dire_series_wins: int = 0

    def to_series_context(self) -> SeriesContext:
        """Unknowns collapse to the neutral value the feature builder expects.

        Not a fabrication: it is recorded as unknown in the database, and this is the point
        where a number has to be produced. Which is exactly why `is_conditional_game` must
        stay NULL upstream rather than defaulting to False there - here the collapse is
        visible and deliberate.
        """
        return SeriesContext(
            series_format=self.series_format or SeriesFormat.BO1,
            game_in_series=self.game_in_series,
            is_conditional_game=bool(self.is_conditional_game),
            radiant_series_wins=self.radiant_series_wins,
            dire_series_wins=self.dire_series_wins,
        )


async def _contexts_for(session: AsyncSession, match_ids: list[int]) -> dict[int, MatchContext]:
    rows = (
        await session.execute(
            select(
                Match.match_id,
                Match.game_in_series,
                Match.is_conditional_game,
                Match.radiant_team_id,
                Series.format,
                Series.team_a_id,
                Series.score_a,
                Series.score_b,
            )
            .join(Series, Series.series_id == Match.series_id, isouter=True)
            .where(Match.match_id.in_(match_ids))
        )
    ).all()

    contexts: dict[int, MatchContext] = {}
    for (
        match_id,
        game_in_series,
        is_conditional,
        radiant_team_id,
        fmt,
        team_a_id,
        score_a,
        score_b,
    ) in rows:
        # Series scores are stored against team A; the snapshot needs them by side.
        radiant_is_a = radiant_team_id is not None and radiant_team_id == team_a_id
        contexts[int(match_id)] = MatchContext(
            series_format=SeriesFormat(fmt) if fmt else None,
            game_in_series=int(game_in_series or 1),
            is_conditional_game=is_conditional,
            radiant_series_wins=int(score_a if radiant_is_a else score_b or 0),
            dire_series_wins=int(score_b if radiant_is_a else score_a or 0),
        )
    return contexts


async def featurize(
    session_factory: async_sessionmaker[AsyncSession],
    batch_size: int = 50,
    limit: int | None = None,
) -> FeaturizeReport:
    """Turn stored match payloads into snapshots."""
    report = FeaturizeReport()
    offset = 0

    while True:
        async with session_factory() as session:
            statement = (
                select(RawMatch.payload)
                .where(RawMatch.source == str(RawSource.OPENDOTA_MATCH))
                .order_by(RawMatch.match_id)
                .offset(offset)
                .limit(batch_size if limit is None else min(batch_size, limit - offset))
            )
            payloads = list((await session.execute(statement)).scalars().all())

        if not payloads:
            break

        await _featurize_batch(session_factory, payloads, report)
        offset += len(payloads)

        if limit is not None and offset >= limit:
            break

    log.info("featurize.done", **report.as_log_fields())
    return report


async def _featurize_batch(
    session_factory: async_sessionmaker[AsyncSession],
    payloads: list[dict[str, Any]],
    report: FeaturizeReport,
) -> None:
    usable: list[dict[str, Any]] = []
    for payload in payloads:
        report.matches_seen += 1
        if payload.get("match_id") is None:
            report.skip("no match_id")
        elif not is_parsed(payload):
            # No per-minute series exists for an unparsed match; there is nothing to unroll.
            report.skip("not parsed")
        elif payload.get("radiant_win") is None:
            report.skip("no outcome to label with")
        elif int(payload.get("duration") or 0) < MIN_DURATION_SECONDS:
            report.skip("shorter than 12 minutes")
        else:
            usable.append(payload)

    if not usable:
        return

    async with session_factory() as session:
        contexts = await _contexts_for(session, [int(p["match_id"]) for p in usable])

        rows: list[dict[str, Any]] = []
        for payload in usable:
            match_id = int(payload["match_id"])
            context = contexts.get(match_id, MatchContext())
            label = bool(payload["radiant_win"])

            for state in iter_snapshots(payload, series=context.to_series_context()):
                rows.append(
                    {
                        "match_id": match_id,
                        "minute": state.minute,
                        "features": build_live_features(state),
                        "radiant_win": label,
                    }
                )
            report.matches_used += 1

        if rows:
            statement = insert(MatchSnapshot).values(rows)
            statement = statement.on_conflict_do_update(
                index_elements=[MatchSnapshot.match_id, MatchSnapshot.minute],
                set_={
                    "features": statement.excluded.features,
                    "radiant_win": statement.excluded.radiant_win,
                },
            )
            await session.execute(statement)
            report.snapshots += len(rows)

        await session.commit()
