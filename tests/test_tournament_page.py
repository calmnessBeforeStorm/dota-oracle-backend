"""Participants and results on the tournament page (F4, spec section 8.1).

The tally is where the Bo2 rules meet the UI: a drawn series is its own outcome, and a
series still being played is not a loss for anybody.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.tournament import outcome_from_maps, participants_from, series_for
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


class TestOutcomeFromMapScore:
    """Reading a finished series off its map score (spec section 5.5).

    The format is what tells you a series has *ended* - two won maps end a Bo3 and not a Bo5
    - so the score cannot be read while anybody is still playing. Once nothing has been
    played for half a day, though, the maps that exist are all the maps there were.

    Measured: 11600 of 13588 series can be read this way, against 1577 carrying a recorded
    winner. The International 2026 is the case that raised it - a mapped Tier 1 tournament
    showing every one of its sixteen teams at 0-0-0 while all 58 of its series had an
    unequal map score.
    """

    SETTLED = BASE - timedelta(days=4)
    JUST_PLAYED = BASE - timedelta(minutes=30)

    def test_a_settled_series_is_decided_by_its_maps(self) -> None:
        assert outcome_from_maps(2, 0, self.SETTLED, BASE)

    def test_a_series_still_being_played_is_not(self) -> None:
        """The whole reason the format matters: 2-0 is a finished Bo3 and a live Bo5."""
        assert not outcome_from_maps(2, 0, self.JUST_PLAYED, BASE)

    def test_a_level_score_is_never_called(self) -> None:
        """1-1 is a drawn Bo2 or an abandoned Bo3, and only the format separates them."""
        assert not outcome_from_maps(1, 1, self.SETTLED, BASE)

    def test_a_series_with_no_maps_is_not_called(self) -> None:
        assert not outcome_from_maps(0, 0, None, BASE)

    def test_the_boundary_is_generous(self) -> None:
        """Twelve hours, well past the four a Bo5 takes. Calling a live series decided is a
        visible error; waiting a few extra hours costs nothing."""
        assert not outcome_from_maps(2, 0, BASE - timedelta(hours=11), BASE)
        assert outcome_from_maps(2, 0, BASE - timedelta(hours=13), BASE)
