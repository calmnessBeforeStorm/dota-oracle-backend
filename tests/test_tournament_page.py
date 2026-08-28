"""Participants and results on the tournament page (F4, spec section 8.1).

The tally is where the Bo2 rules meet the UI: a drawn series is its own outcome, and a
series still being played is not a loss for anybody.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.tournament import participants_from, series_for
from app.db.models.matches import Match, Series
from app.db.models.reference import League, Team
from app.schemas.common import SeriesResult, TeamBrief

LEAGUE = 500
ALPHA, BETA, GAMMA = 1, 2, 3
BASE = datetime(2026, 8, 1, tzinfo=UTC)


def result(**overrides: object) -> SeriesResult:
    defaults: dict[str, object] = {
        "series_id": 1,
        "team_a": TeamBrief(team_id=ALPHA, name="Alpha"),
        "team_b": TeamBrief(team_id=BETA, name="Beta"),
        "score_a": 2,
        "score_b": 0,
        "winner_team_id": ALPHA,
        "is_draw": False,
        "maps": 2,
    }
    return SeriesResult(**{**defaults, **overrides})  # type: ignore[arg-type]


class TestParticipants:
    def test_a_win_and_a_loss_are_recorded_on_both_sides(self) -> None:
        table = participants_from([result()])

        alpha = next(p for p in table if p.team.team_id == ALPHA)
        beta = next(p for p in table if p.team.team_id == BETA)
        assert (alpha.series_won, alpha.series_lost) == (1, 0)
        assert (beta.series_won, beta.series_lost) == (0, 1)

    def test_maps_are_counted_from_the_series_score(self) -> None:
        alpha = next(p for p in participants_from([result()]) if p.team.team_id == ALPHA)

        assert (alpha.maps_won, alpha.maps_lost) == (2, 0)

    def test_a_draw_is_its_own_outcome_not_half_a_win(self) -> None:
        """Bo2 ends 1-1, and rounding that into wins is exactly what section 5.5 forbids."""
        table = participants_from(
            [result(score_a=1, score_b=1, winner_team_id=None, is_draw=True, maps=2)]
        )

        for row in table:
            assert (row.series_won, row.series_lost, row.series_drawn) == (0, 0, 1)
            assert (row.maps_won, row.maps_lost) == (1, 1)

    def test_an_unfinished_series_counts_for_nobody(self) -> None:
        """No winner and no draw flag means it has not finished. Counting it either way
        would invent a result."""
        table = participants_from(
            [result(score_a=1, score_b=0, winner_team_id=None, is_draw=False, maps=1)]
        )

        for row in table:
            assert (row.series_won, row.series_lost, row.series_drawn) == (0, 0, 0)

    def test_records_accumulate_across_series(self) -> None:
        table = participants_from(
            [
                result(series_id=1, winner_team_id=ALPHA),
                result(
                    series_id=2,
                    team_b=TeamBrief(team_id=GAMMA, name="Gamma"),
                    score_a=0,
                    score_b=2,
                    winner_team_id=GAMMA,
                ),
            ]
        )

        alpha = next(p for p in table if p.team.team_id == ALPHA)
        assert (alpha.series_won, alpha.series_lost) == (1, 1)
        assert (alpha.maps_won, alpha.maps_lost) == (2, 2)

    def test_the_table_is_ordered_by_record(self) -> None:
        table = participants_from(
            [
                result(series_id=1, winner_team_id=ALPHA),
                result(
                    series_id=2,
                    team_a=TeamBrief(team_id=GAMMA, name="Gamma"),
                    winner_team_id=GAMMA,
                ),
                result(
                    series_id=3, team_a=TeamBrief(team_id=GAMMA, name="Gamma"), winner_team_id=GAMMA
                ),
            ]
        )

        assert next(p.team.team_id for p in table) == GAMMA  # two wins beats one

    def test_a_series_with_an_unregistered_side_is_skipped_for_that_side(self) -> None:
        """`team_id` is null when a side played as an unregistered stack."""
        table = participants_from([result(team_b=TeamBrief())])

        assert [p.team.team_id for p in table] == [ALPHA]


class TestSeriesQuery:
    async def test_series_come_back_oldest_first_with_their_teams(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        session.add_all(
            [
                League(league_id=LEAGUE, name="Test Cup"),
                Team(team_id=ALPHA, name="Alpha"),
                Team(team_id=BETA, name="Beta"),
            ]
        )
        await session.flush()
        session.add_all(
            [
                Series(
                    series_id=10,
                    league_id=LEAGUE,
                    team_a_id=ALPHA,
                    team_b_id=BETA,
                    score_a=2,
                    score_b=1,
                    winner_team_id=ALPHA,
                ),
                Series(
                    series_id=11,
                    league_id=LEAGUE,
                    team_a_id=BETA,
                    team_b_id=ALPHA,
                    score_a=1,
                    score_b=1,
                    is_draw=True,
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                Match(
                    match_id=900 + i,
                    league_id=LEAGUE,
                    series_id=11,
                    start_time=BASE + timedelta(hours=i),
                )
                for i in range(2)
            ]
            + [
                Match(
                    match_id=800 + i,
                    league_id=LEAGUE,
                    series_id=10,
                    start_time=BASE - timedelta(days=1, hours=i),
                )
                for i in range(3)
            ]
        )
        await session.commit()

        results = await series_for(session, LEAGUE)

        assert [r.series_id for r in results] == [10, 11]  # oldest first
        assert results[0].team_a.name == "Alpha"
        assert results[0].maps == 3
        assert results[1].is_draw is True

    async def test_a_series_with_no_maps_still_appears(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """Scheduled but unplayed, or played maps we have not fetched. Dropping it would
        make the results list silently disagree with the series count above it."""
        session.add_all([League(league_id=LEAGUE, name="Test Cup"), Team(team_id=ALPHA)])
        await session.flush()
        session.add(Series(series_id=12, league_id=LEAGUE, team_a_id=ALPHA))
        await session.commit()

        results = await series_for(session, LEAGUE)

        assert [r.series_id for r in results] == [12]
        assert results[0].maps == 0
        assert results[0].played_at is None

    async def test_maps_come_back_in_play_order(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """The UI links to the first map of a series, so "first" has to mean the one played
        first rather than whichever id the aggregate happened to see."""
        session.add_all([League(league_id=LEAGUE, name="Test Cup"), Team(team_id=ALPHA)])
        await session.flush()
        session.add(Series(series_id=13, league_id=LEAGUE, team_a_id=ALPHA))
        await session.flush()
        session.add_all(
            [
                Match(
                    match_id=700,
                    league_id=LEAGUE,
                    series_id=13,
                    start_time=BASE + timedelta(hours=2),
                ),
                Match(match_id=701, league_id=LEAGUE, series_id=13, start_time=BASE),
                Match(
                    match_id=702,
                    league_id=LEAGUE,
                    series_id=13,
                    start_time=BASE + timedelta(hours=1),
                ),
            ]
        )
        await session.commit()

        results = await series_for(session, LEAGUE)

        assert results[0].match_ids == [701, 702, 700]

    async def test_a_series_with_no_maps_has_an_empty_id_list(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """The outer join yields a NULL row, which must not become a match id of None."""
        session.add_all([League(league_id=LEAGUE, name="Test Cup"), Team(team_id=ALPHA)])
        await session.flush()
        session.add(Series(series_id=14, league_id=LEAGUE, team_a_id=ALPHA))
        await session.commit()

        results = await series_for(session, LEAGUE)

        assert results[0].match_ids == []
