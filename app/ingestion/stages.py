"""Attaching series to tournament stages, and resolving what that implies (spec section 5.5).

Formats live on the stage, series live in our own data, and nothing connected the two: every
series carried `format = NULL` and every map `is_conditional_game = NULL`. This is the step
that closes it, and it is where Bo2 finally becomes visible - a 1-1 in a Bo2 group stage
stops being "no winner yet" and becomes a draw.

Uses no network at all: stages come from an earlier Liquipedia sync, series from the
matches we hold.

A series is attached to the stage whose window it was played in. Where several stages of the
same tournament overlap - The International runs both its Bo2 phase and its Bo3 seeding
decider across the same four days - the map count can rule some out, since a three-map
series cannot be a Bo2. When more than one candidate survives that, the series is left
unattached: guessing here would put a fabricated format straight into the training data.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.db.models.enums import SeriesFormat
from app.db.models.matches import Match, Series
from app.db.models.reference import TournamentStage
from app.domain.series import is_conditional_game, series_outcome
from app.ingestion.repository import utcnow

log = get_logger(__name__)

#: Stages state calendar days while matches carry timestamps, and a series can start late
#: in the evening of the last day. A day either side absorbs that without letting a series
#: drift into a neighbouring stage.
WINDOW_TOLERANCE = timedelta(days=1)


@dataclass
class LinkReport:
    series_seen: int = 0
    linked: int = 0
    formats_set: int = 0
    conditional_flags_set: int = 0
    outcomes_resolved: int = 0
    draws_found: int = 0
    #: Series whose dates matched several stages that the map count could not separate.
    ambiguous: int = 0
    no_stage: int = 0
    reasons: dict[str, int] = field(default_factory=dict)

    def as_log_fields(self) -> dict[str, Any]:
        return {
            "series_seen": self.series_seen,
            "linked": self.linked,
            "formats_set": self.formats_set,
            "conditional_flags_set": self.conditional_flags_set,
            "outcomes_resolved": self.outcomes_resolved,
            "draws_found": self.draws_found,
            "ambiguous": self.ambiguous,
            "no_stage": self.no_stage,
        }


@dataclass(frozen=True)
class StageWindow:
    stage_id: int
    league_id: int
    name: str
    default_format: SeriesFormat
    start: date
    end: date

    def covers(self, day: date) -> bool:
        return self.start - WINDOW_TOLERANCE <= day <= self.end + WINDOW_TOLERANCE


def pick_stage(
    candidates: list[StageWindow], played_on: date, map_count: int
) -> tuple[StageWindow | None, str]:
    """Choose the stage a series belongs to, or explain why it cannot be chosen."""
    covering = [stage for stage in candidates if stage.covers(played_on)]
    if not covering:
        return None, "no stage covers the dates"
    if len(covering) == 1:
        return covering[0], "single stage covers the dates"

    # More maps than a format allows rules that format out entirely.
    possible = [stage for stage in covering if stage.default_format.max_games >= map_count]
    if len(possible) == 1:
        return possible[0], "map count ruled out the other stages"
    if not possible:
        return None, "map count fits no covering stage"

    # Stages that agree on the format are interchangeable for our purposes.
    formats = {stage.default_format for stage in possible}
    if len(formats) == 1:
        return possible[0], "overlapping stages agree on the format"

    return None, "several stages overlap and disagree on the format"


async def _stage_windows(session: AsyncSession) -> dict[int, list[StageWindow]]:
    rows = (
        await session.execute(
            select(
                TournamentStage.stage_id,
                TournamentStage.league_id,
                TournamentStage.name,
                TournamentStage.default_format,
                TournamentStage.starts_at,
                TournamentStage.ends_at,
            ).where(TournamentStage.starts_at.is_not(None), TournamentStage.ends_at.is_not(None))
        )
    ).all()

    windows: dict[int, list[StageWindow]] = {}
    for stage_id, league_id, name, fmt, starts, ends in rows:
        windows.setdefault(int(league_id), []).append(
            StageWindow(
                stage_id=int(stage_id),
                league_id=int(league_id),
                name=str(name),
                default_format=SeriesFormat(str(fmt)),
                start=starts.date(),
                end=ends.date(),
            )
        )
    return windows


async def link_series_to_stages(
    session_factory: async_sessionmaker[AsyncSession],
) -> LinkReport:
    """Attach series to stages, then set the format, conditionality and series outcome."""
    report = LinkReport()

    async with session_factory() as session:
        windows = await _stage_windows(session)
        if not windows:
            log.info("stages.link_skipped", reason="no stage has dates yet")
            return report

        series_rows = (
            await session.execute(
                select(
                    Series.series_id,
                    Series.league_id,
                    Series.team_a_id,
                    Series.team_b_id,
                    Series.score_a,
                    Series.score_b,
                    func.min(Match.start_time),
                    func.count(Match.match_id),
                )
                .join(Match, Match.series_id == Series.series_id)
                .where(Series.league_id.in_(windows.keys()))
                .group_by(
                    Series.series_id,
                    Series.league_id,
                    Series.team_a_id,
                    Series.team_b_id,
                    Series.score_a,
                    Series.score_b,
                )
            )
        ).all()

        for (
            series_id,
            league_id,
            team_a_id,
            team_b_id,
            score_a,
            score_b,
            first_match,
            map_count,
        ) in series_rows:
            report.series_seen += 1
            if first_match is None:
                continue

            stage, reason = pick_stage(
                windows.get(int(league_id), []), first_match.date(), int(map_count)
            )
            report.reasons[reason] = report.reasons.get(reason, 0) + 1

            if stage is None:
                if "several stages" in reason or "fits no" in reason:
                    report.ambiguous += 1
                else:
                    report.no_stage += 1
                continue

            report.linked += 1
            report.formats_set += 1

            values: dict[str, Any] = {
                "stage_id": stage.stage_id,
                "format": stage.default_format.value,
                "updated_at": utcnow(),
            }

            # With the format known, a score finally means something: 1-1 in a Bo2 is a
            # draw, the same score in a Bo3 is an unfinished series.
            outcome = series_outcome(stage.default_format, int(score_a), int(score_b))
            if outcome.is_decided:
                report.outcomes_resolved += 1
                values["is_draw"] = outcome.is_draw
                if outcome.is_draw:
                    report.draws_found += 1
                    values["winner_team_id"] = None
                elif outcome.winner_is_a is not None:
                    values["winner_team_id"] = team_a_id if outcome.winner_is_a else team_b_id

            await session.execute(
                update(Series).where(Series.series_id == series_id).values(**values)
            )

            report.conditional_flags_set += await _set_conditional_flags(
                session, int(series_id), stage.default_format
            )

        await session.commit()

    log.info("stages.linked", **report.as_log_fields())
    return report


async def _set_conditional_flags(session: AsyncSession, series_id: int, fmt: SeriesFormat) -> int:
    """Mark the maps that were only played because of the series score.

    Fed to the model alongside `game_in_series`; without it the model learns the format
    artifact instead of the effect (spec section 5.5).
    """
    rows = (
        await session.execute(
            select(Match.match_id, Match.game_in_series).where(
                Match.series_id == series_id, Match.game_in_series.is_not(None)
            )
        )
    ).all()

    updated = 0
    for match_id, game_in_series in rows:
        await session.execute(
            update(Match)
            .where(Match.match_id == match_id)
            .values(is_conditional_game=is_conditional_game(fmt, int(game_in_series)))
        )
        updated += 1
    return updated
