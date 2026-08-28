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

**One source, and only one.** Snapshots are built from STRATZ payloads. OpenDota's
per-minute series reports *earned gold* while STRATZ reports *net worth*, which is also what
the live scoreboard the serve path reads reports. Feeding both into this table would put two
different quantities in the same column and teach the model a source artifact that no metric
would reveal. The measurements are in
docs/superpowers/specs/2026-08-27-stratz-adapter-design.md.
"""

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.db.models.enums import SeriesFormat
from app.db.models.matches import Match, Series
from app.db.models.raw import RawMatch
from app.db.models.training import MatchPrematch, MatchSnapshot
from app.features.adapters.stratz import is_parsed, iter_snapshots
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
    deleted: int = 0
    skipped: dict[str, int] = field(default_factory=dict)

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1

    def as_log_fields(self) -> dict[str, Any]:
        return {
            "matches_seen": self.matches_seen,
            "matches_used": self.matches_used,
            "snapshots": self.snapshots,
            "deleted": self.deleted,
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


async def _prematch_for(
    session: AsyncSession, match_ids: list[int]
) -> dict[int, tuple[dict[str, float], float]]:
    """Pre-match features and prior per map, from the chronological sweep.

    Absent for any map whose rosters we have not fetched. Missing is left missing: the
    feature builder defaults these differences to zero, which is the neutral value, whereas
    inventing a skill gap would not be.
    """
    rows = (
        await session.execute(
            select(
                MatchPrematch.match_id, MatchPrematch.features, MatchPrematch.prematch_prior
            ).where(MatchPrematch.match_id.in_(match_ids))
        )
    ).all()
    return {int(match_id): (dict(features), float(prior)) for match_id, features, prior in rows}


async def contexts_for(session: AsyncSession, match_ids: list[int]) -> dict[int, MatchContext]:
    """Series-level facts about each map, as they stood **before** that map was played.

    The score must not be read off `series.score_a` / `score_b`. Those are the *final*
    scores, so a map from a series that ended 2-0 would announce "+2" from its first
    snapshot - which is only possible if that side won the very map being predicted. That
    was measured on the real table: `series_wins_diff = +2` meant Radiant won 100% of the
    time (spec section 12).

    It is reconstructed instead from the sibling maps that came earlier in the series, and
    attributed to *teams* rather than to sides: the sides swap between maps, and a score
    that stays with the side reads as the opposite of what happened.
    """
    rows = (
        await session.execute(
            select(
                Match.match_id,
                Match.series_id,
                Match.game_in_series,
                Match.is_conditional_game,
                Match.radiant_team_id,
                Match.dire_team_id,
                Series.format,
            )
            .join(Series, Series.series_id == Match.series_id, isouter=True)
            .where(Match.match_id.in_(match_ids))
        )
    ).all()

    series_ids = {int(r[1]) for r in rows if r[1] is not None}
    earlier = await _wins_before(session, series_ids)

    contexts: dict[int, MatchContext] = {}
    for (
        match_id,
        series_id,
        game_in_series,
        is_conditional,
        radiant_team_id,
        dire_team_id,
        fmt,
    ) in rows:
        position = int(game_in_series or 1)
        tally = earlier.get(int(series_id), {}) if series_id is not None else {}
        radiant_wins = sum(
            1
            for (team_id, at) in tally
            if at < position and team_id == radiant_team_id and tally[(team_id, at)]
        )
        dire_wins = sum(
            1
            for (team_id, at) in tally
            if at < position and team_id == dire_team_id and tally[(team_id, at)]
        )
        contexts[int(match_id)] = MatchContext(
            series_format=SeriesFormat(fmt) if fmt else None,
            game_in_series=position,
            is_conditional_game=is_conditional,
            radiant_series_wins=radiant_wins,
            dire_series_wins=dire_wins,
        )
    return contexts


async def _wins_before(
    session: AsyncSession, series_ids: set[int]
) -> dict[int, dict[tuple[int, int], bool]]:
    """Per series: `(winning_team_id, game_in_series) -> True` for every decided map.

    A map still in progress has `radiant_win` NULL and contributes nothing. Counting it
    either way would invent a result (invariant 12).
    """
    if not series_ids:
        return {}

    rows = (
        await session.execute(
            select(
                Match.series_id,
                Match.game_in_series,
                Match.radiant_team_id,
                Match.dire_team_id,
                Match.radiant_win,
            ).where(Match.series_id.in_(series_ids), Match.radiant_win.is_not(None))
        )
    ).all()

    tally: dict[int, dict[tuple[int, int], bool]] = {}
    for series_id, position, radiant_team_id, dire_team_id, radiant_win in rows:
        winner = radiant_team_id if radiant_win else dire_team_id
        if winner is None or position is None:
            continue
        tally.setdefault(int(series_id), {})[(int(winner), int(position))] = True
    return tally


async def featurize(
    session_factory: async_sessionmaker[AsyncSession],
    batch_size: int = 50,
    limit: int | None = None,
    rebuild: bool = False,
) -> FeaturizeReport:
    """Turn stored match payloads into snapshots.

    `rebuild` empties the table first. Rows are only ever upserted, so without it a change
    to the feature set or to the source leaves the old rows in place next to the new ones -
    which is how a table ends up holding two different quantities in the same column. The
    rows are fully derived from `raw_matches`, so dropping them costs a re-run and nothing
    else.
    """
    report = FeaturizeReport()
    offset = 0

    if rebuild:
        async with session_factory() as session:
            existing = await session.execute(select(func.count()).select_from(MatchSnapshot))
            report.deleted = int(existing.scalar_one())
            await session.execute(delete(MatchSnapshot))
            await session.commit()
        log.info("featurize.cleared", deleted=report.deleted)

    while True:
        async with session_factory() as session:
            # Joined to `matches`, and that join is load-bearing. `match_snapshots.match_id`
            # is a foreign key, so a payload whose match has not been normalised yet aborts
            # the whole insert - and payloads arrive between pipeline runs on their own now
            # that `resolve-outcomes` is on a cron. One out-of-order fetch used to take a
            # forty-minute rebuild down with it.
            statement = (
                select(RawMatch.payload)
                .join(Match, Match.match_id == RawMatch.match_id)
                .where(RawMatch.source == str(RawSource.STRATZ_MATCH))
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
        if payload.get("id") is None:
            report.skip("no match_id")
        elif not is_parsed(payload):
            # No per-minute series exists for an unparsed match; there is nothing to unroll.
            report.skip("not parsed")
        elif payload.get("didRadiantWin") is None:
            report.skip("no outcome to label with")
        elif int(payload.get("durationSeconds") or 0) < MIN_DURATION_SECONDS:
            report.skip("shorter than 12 minutes")
        else:
            usable.append(payload)

    if not usable:
        return

    async with session_factory() as session:
        match_ids = [int(p["id"]) for p in usable]
        contexts = await contexts_for(session, match_ids)
        prematch = await _prematch_for(session, match_ids)

        rows: list[dict[str, Any]] = []
        for payload in usable:
            match_id = int(payload["id"])
            context = contexts.get(match_id, MatchContext())
            label = bool(payload["didRadiantWin"])
            prematch_features, prior = prematch.get(match_id, ({}, 0.5))

            for state in iter_snapshots(
                payload,
                series=context.to_series_context(),
                prematch=prematch_features,
                prematch_prior=prior,
            ):
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
