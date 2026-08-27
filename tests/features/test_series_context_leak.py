"""The series score in a snapshot must be the score BEFORE that map (spec section 12).

Found on 27.08.2026 by a trained model that predicted 87.9% of holdout maps correctly at
minute zero, before anything had happened. `series_wins_diff` was carrying the *final*
series score, so a map from a series that ended 2-0 announced "+2" from its first snapshot -
which is only possible if that side won the very map being predicted. Measured over the
whole table: `series_wins_diff = +2` meant Radiant won 100% of the time, `-2` meant 0.6%.

`tests/features/test_leakage.py` cannot catch this by construction. It truncates the match
payload and re-runs the adapter, but the series context is not in the payload - it arrives
from the normalized tables and is identical in both runs.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.matches import Match, Series
from app.db.models.reference import League, Team
from app.features.featurize import contexts_for

BASE = datetime(2026, 8, 1, tzinfo=UTC)
TEAM_A, TEAM_B = 11, 22


async def seed_series(session: AsyncSession, results: list[tuple[int, bool]]) -> list[int]:
    """One series whose maps are `(radiant_team_id, radiant_win)`, in order.

    Sides swap between maps in real series, which is exactly where a naive fix breaks.
    """
    session.add_all([League(league_id=1, name="test"), Team(team_id=TEAM_A), Team(team_id=TEAM_B)])
    await session.flush()
    session.add(
        Series(series_id=1, valve_series_id=1, league_id=1, team_a_id=TEAM_A, team_b_id=TEAM_B)
    )
    await session.flush()

    match_ids = []
    for index, (radiant_team_id, radiant_win) in enumerate(results, start=1):
        match_id = 500 + index
        match_ids.append(match_id)
        session.add(
            Match(
                match_id=match_id,
                series_id=1,
                game_in_series=index,
                radiant_team_id=radiant_team_id,
                dire_team_id=TEAM_B if radiant_team_id == TEAM_A else TEAM_A,
                radiant_win=radiant_win,
                start_time=BASE + timedelta(hours=index),
            )
        )
    await session.commit()
    return match_ids


class TestPreMapScore:
    async def test_the_first_map_of_a_series_starts_at_nil_nil(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        ids = await seed_series(session, [(TEAM_A, True), (TEAM_A, True)])

        contexts = await contexts_for(session, ids)

        first = contexts[ids[0]]
        assert (first.radiant_series_wins, first.dire_series_wins) == (0, 0)

    async def test_the_second_map_sees_only_the_first(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """Team A wins map 1. On map 2 the score is 1-0 to A, never the final 2-0."""
        ids = await seed_series(session, [(TEAM_A, True), (TEAM_A, True)])

        second = (await contexts_for(session, ids))[ids[1]]

        assert (second.radiant_series_wins, second.dire_series_wins) == (1, 0)

    async def test_a_side_swap_moves_the_score_with_the_team(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """Team A wins map 1 as Radiant, then plays map 2 as Dire. The score must follow the
        team, not the side - otherwise it reads as 0-1 against them."""
        ids = await seed_series(session, [(TEAM_A, True), (TEAM_B, True)])

        second = (await contexts_for(session, ids))[ids[1]]

        # Map 2: Radiant is team B, who has 0 wins; Dire is team A, who has 1.
        assert (second.radiant_series_wins, second.dire_series_wins) == (0, 1)

    async def test_a_decider_sees_one_all(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        ids = await seed_series(session, [(TEAM_A, True), (TEAM_A, False), (TEAM_A, True)])

        third = (await contexts_for(session, ids))[ids[2]]

        assert (third.radiant_series_wins, third.dire_series_wins) == (1, 1)

    async def test_the_score_never_counts_the_map_being_predicted(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """The whole point. Total wins seen must always be one less than the map number."""
        ids = await seed_series(session, [(TEAM_A, True), (TEAM_B, True), (TEAM_A, True)])

        contexts = await contexts_for(session, ids)

        for position, match_id in enumerate(ids):
            context = contexts[match_id]
            assert context.radiant_series_wins + context.dire_series_wins == position

    async def test_a_standalone_map_has_no_series_score(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        session.add(
            Match(
                match_id=900,
                series_id=None,
                game_in_series=1,
                radiant_team_id=TEAM_A,
                dire_team_id=TEAM_B,
                radiant_win=True,
                start_time=BASE,
            )
        )
        await session.commit()

        context = (await contexts_for(session, [900]))[900]

        assert (context.radiant_series_wins, context.dire_series_wins) == (0, 0)

    async def test_an_unfinished_earlier_map_is_not_counted_as_a_loss(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """`radiant_win` is NULL while a map is still being played. Counting it either way
        would invent a result (invariant 12)."""
        session.add_all(
            [League(league_id=1, name="test"), Team(team_id=TEAM_A), Team(team_id=TEAM_B)]
        )
        await session.flush()
        session.add(
            Series(series_id=1, valve_series_id=1, league_id=1, team_a_id=TEAM_A, team_b_id=TEAM_B)
        )
        await session.flush()
        for index, win in ((1, None), (2, True)):
            session.add(
                Match(
                    match_id=600 + index,
                    series_id=1,
                    game_in_series=index,
                    radiant_team_id=TEAM_A,
                    dire_team_id=TEAM_B,
                    radiant_win=win,
                    start_time=BASE + timedelta(hours=index),
                )
            )
        await session.commit()

        context = (await contexts_for(session, [602]))[602]

        assert (context.radiant_series_wins, context.dire_series_wins) == (0, 0)
