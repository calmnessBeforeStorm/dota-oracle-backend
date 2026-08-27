"""Attaching series to stages, and what that resolves (spec section 5.5).

This is the step where a format finally reaches the data, so it is also where a Bo2 draw
finally becomes expressible. No live Bo2 tournament is in our slice yet, so the Bo2 path is
proved here rather than left to be discovered when one arrives.
"""

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.enums import SeriesFormat, StageType
from app.db.models.matches import Match, Series
from app.db.models.reference import League, Team, TournamentStage
from app.ingestion.stages import StageWindow, link_series_to_stages, pick_stage

BASE = datetime(2026, 5, 13, 12, 0, tzinfo=UTC)


def window(name: str, fmt: SeriesFormat, start: date, end: date, stage_id: int = 1) -> StageWindow:
    return StageWindow(
        stage_id=stage_id, league_id=1, name=name, default_format=fmt, start=start, end=end
    )


class TestPickStage:
    def test_single_covering_stage_wins(self) -> None:
        stages = [
            window("Group", SeriesFormat.BO2, date(2026, 5, 1), date(2026, 5, 5)),
            window("Playoffs", SeriesFormat.BO3, date(2026, 5, 8), date(2026, 5, 10), 2),
        ]
        chosen, _ = pick_stage(stages, date(2026, 5, 3), map_count=2)
        assert chosen is not None
        assert chosen.name == "Group"

    def test_map_count_separates_overlapping_stages(self) -> None:
        """The International runs a Bo2 phase and a Bo3 decider across the same four days;
        a three-map series cannot be the Bo2 one."""
        stages = [
            window("Phase One", SeriesFormat.BO2, date(2026, 5, 1), date(2026, 5, 4)),
            window("Phase Two", SeriesFormat.BO3, date(2026, 5, 1), date(2026, 5, 4), 2),
        ]
        chosen, reason = pick_stage(stages, date(2026, 5, 2), map_count=3)
        assert chosen is not None
        assert chosen.default_format is SeriesFormat.BO3
        assert "map count" in reason

    def test_overlapping_stages_that_disagree_are_refused(self) -> None:
        """Two maps fit both a Bo2 and a Bo3. Picking either would put a fabricated format
        straight into the training data."""
        stages = [
            window("Phase One", SeriesFormat.BO2, date(2026, 5, 1), date(2026, 5, 4)),
            window("Phase Two", SeriesFormat.BO3, date(2026, 5, 1), date(2026, 5, 4), 2),
        ]
        chosen, reason = pick_stage(stages, date(2026, 5, 2), map_count=2)
        assert chosen is None
        assert "disagree" in reason

    def test_overlapping_stages_that_agree_are_fine(self) -> None:
        stages = [
            window("A", SeriesFormat.BO3, date(2026, 5, 1), date(2026, 5, 4)),
            window("B", SeriesFormat.BO3, date(2026, 5, 1), date(2026, 5, 4), 2),
        ]
        chosen, _ = pick_stage(stages, date(2026, 5, 2), map_count=2)
        assert chosen is not None

    def test_series_outside_every_window(self) -> None:
        stages = [window("Group", SeriesFormat.BO3, date(2026, 5, 1), date(2026, 5, 4))]
        chosen, reason = pick_stage(stages, date(2026, 6, 20), map_count=2)
        assert chosen is None
        assert "no stage covers" in reason

    @pytest.mark.parametrize("day", [date(2026, 4, 30), date(2026, 5, 5)])
    def test_a_day_of_tolerance_either_side(self, day: date) -> None:
        """Stages state calendar days while matches carry timestamps."""
        stages = [window("Group", SeriesFormat.BO3, date(2026, 5, 1), date(2026, 5, 4))]
        chosen, _ = pick_stage(stages, day, map_count=2)
        assert chosen is not None


async def seed(
    session: AsyncSession,
    stage_format: SeriesFormat,
    maps: list[tuple[int, bool]],
) -> None:
    """One league, one stage, one series with the given maps as (game_in_series, radiant_win)."""
    now = datetime.now(UTC)
    session.add(League(league_id=1, name="Test League", created_at=now, updated_at=now))
    # The series table has real foreign keys onto teams.
    session.add(Team(team_id=10, name="Alpha", created_at=now, updated_at=now))
    session.add(Team(team_id=20, name="Beta", created_at=now, updated_at=now))
    await session.flush()
    session.add(
        TournamentStage(
            league_id=1,
            name="Group Stage",
            stage_type=StageType.GROUP.value,
            default_format=stage_format.value,
            starts_at=BASE,
            ends_at=BASE,
            created_at=now,
            updated_at=now,
        )
    )
    session.add(
        Series(
            series_id=1,
            league_id=1,
            valve_series_id=100,
            team_a_id=None,
            team_b_id=None,
            score_a=0,
            score_b=0,
            created_at=now,
            updated_at=now,
        )
    )
    await session.flush()
    for index, (game_in_series, radiant_win) in enumerate(maps):
        session.add(
            Match(
                match_id=1000 + index,
                league_id=1,
                series_id=1,
                game_in_series=game_in_series,
                radiant_team_id=10,
                dire_team_id=20,
                radiant_win=radiant_win,
                start_time=BASE,
                duration=2000,
                is_parsed=True,
                created_at=now,
                updated_at=now,
            )
        )
    await session.commit()


class TestLinking:
    async def test_sets_format_and_conditionality(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        await seed(session, SeriesFormat.BO3, [(1, True), (2, False), (3, True)])

        report = await link_series_to_stages(sessionmaker)

        assert report.linked == 1
        await session.commit()
        series = (await session.execute(select(Series))).scalar_one()
        assert series.format == SeriesFormat.BO3

        flags = dict(
            (await session.execute(select(Match.game_in_series, Match.is_conditional_game))).all()
        )
        # Bo3 game 3 only happens at 1-1, so it is conditional; games 1 and 2 are not.
        assert flags == {1: False, 2: False, 3: True}

    async def test_bo2_one_one_becomes_a_draw(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """The whole point of section 5.5: before the format is known, 1-1 is
        indistinguishable from an unfinished series."""
        await seed(session, SeriesFormat.BO2, [(1, True), (2, False)])
        async with sessionmaker() as writer:
            await writer.execute(
                Series.__table__.update().values(score_a=1, score_b=1, team_a_id=10, team_b_id=20)
            )
            await writer.commit()

        report = await link_series_to_stages(sessionmaker)

        assert report.draws_found == 1
        await session.commit()
        series = (await session.execute(select(Series))).scalar_one()
        assert series.format == SeriesFormat.BO2
        assert series.is_draw is True
        assert series.winner_team_id is None

    async def test_bo2_second_map_is_never_conditional(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """Game 2 of a Bo2 is always played - that is what makes Bo2 unbiased material."""
        await seed(session, SeriesFormat.BO2, [(1, True), (2, True)])

        await link_series_to_stages(sessionmaker)

        await session.commit()
        flags = dict(
            (await session.execute(select(Match.game_in_series, Match.is_conditional_game))).all()
        )
        assert flags == {1: False, 2: False}

    async def test_decided_series_gets_its_winner(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        await seed(session, SeriesFormat.BO3, [(1, True), (2, True)])
        async with sessionmaker() as writer:
            await writer.execute(
                Series.__table__.update().values(score_a=2, score_b=0, team_a_id=10, team_b_id=20)
            )
            await writer.commit()

        await link_series_to_stages(sessionmaker)

        await session.commit()
        series = (await session.execute(select(Series))).scalar_one()
        assert series.winner_team_id == 10
        assert series.is_draw is False

    async def test_nothing_to_link_without_stage_dates(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """A stage whose dates were never parsed cannot claim anything."""
        await seed(session, SeriesFormat.BO3, [(1, True)])
        async with sessionmaker() as writer:
            await writer.execute(
                TournamentStage.__table__.update().values(starts_at=None, ends_at=None)
            )
            await writer.commit()

        report = await link_series_to_stages(sessionmaker)

        assert report.linked == 0
        await session.commit()
        series = (await session.execute(select(Series))).scalar_one()
        assert series.format is None
