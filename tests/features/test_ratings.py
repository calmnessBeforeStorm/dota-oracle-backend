"""Point-in-time player ratings (spec sections 4.3, 6.2, 12).

The rating stored against a match must be the rating as it stood BEFORE that match. Get the
order wrong and every pre-match feature quietly knows the result of the game it describes -
the same failure as reading the final building state into an early snapshot, but harder to
notice because nothing about the numbers looks unusual.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.matches import Match, MatchPlayer
from app.db.models.reference import League, Player, Team
from app.db.models.training import PlayerRating
from app.features.ratings import (
    ENV,
    PlayerSkill,
    rebuild_player_ratings,
    team_skill,
)

BASE = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)

RADIANT_ACCOUNTS = [101, 102, 103, 104, 105]
DIRE_ACCOUNTS = [201, 202, 203, 204, 205]


async def seed_matches(session: AsyncSession, results: list[bool]) -> None:
    """One league, ten fixed players, a run of matches with the given radiant outcomes."""
    now = datetime.now(UTC)
    session.add(League(league_id=1, name="Test", created_at=now, updated_at=now))
    session.add(Team(team_id=10, name="Radiant Side", created_at=now, updated_at=now))
    session.add(Team(team_id=20, name="Dire Side", created_at=now, updated_at=now))
    for account_id in RADIANT_ACCOUNTS + DIRE_ACCOUNTS:
        session.add(Player(account_id=account_id, created_at=now, updated_at=now))
    await session.flush()

    for index, radiant_win in enumerate(results):
        match_id = 1000 + index
        session.add(
            Match(
                match_id=match_id,
                league_id=1,
                radiant_team_id=10,
                dire_team_id=20,
                radiant_win=radiant_win,
                start_time=BASE + timedelta(days=index),
                duration=2000,
                is_parsed=True,
                created_at=now,
                updated_at=now,
            )
        )
        await session.flush()
        for slot, account_id in enumerate(RADIANT_ACCOUNTS + DIRE_ACCOUNTS):
            session.add(
                MatchPlayer(
                    match_id=match_id,
                    player_slot=slot if slot < 5 else 123 + slot,
                    account_id=account_id,
                    hero_id=1 + slot,
                    is_radiant=slot < 5,
                )
            )
    await session.commit()


class TestPointInTime:
    async def test_first_appearance_carries_no_knowledge(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """Measured on the real data too: all 166 first appearances sit at the default."""
        await seed_matches(session, [True, True, True])
        await rebuild_player_ratings(sessionmaker)
        await session.commit()

        first = (
            await session.execute(
                select(PlayerRating)
                .where(PlayerRating.account_id == RADIANT_ACCOUNTS[0])
                .order_by(PlayerRating.as_of_time)
                .limit(1)
            )
        ).scalar_one()

        assert first.games == 0
        assert first.mu == pytest.approx(ENV.mu)
        assert first.sigma == pytest.approx(ENV.sigma)

    async def test_a_match_does_not_rate_itself(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """The rating written for match two must be the state after match one, not after
        match two - otherwise the feature knows its own label."""
        await seed_matches(session, [True, True])
        await rebuild_player_ratings(sessionmaker)
        await session.commit()

        ratings = (
            await session.execute(
                select(PlayerRating.as_of_match_id, PlayerRating.mu, PlayerRating.games)
                .where(PlayerRating.account_id == RADIANT_ACCOUNTS[0])
                .order_by(PlayerRating.as_of_time)
            )
        ).all()

        assert [games for _, _, games in ratings] == [0, 1]
        first_mu, second_mu = ratings[0][1], ratings[1][1]
        assert first_mu == pytest.approx(ENV.mu)
        assert second_mu > first_mu  # they won match one

    async def test_truncating_history_leaves_earlier_ratings_untouched(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """The leakage check in its strongest form: ratings for the first two matches must
        not change when later matches are removed."""
        await seed_matches(session, [True, False, True, False])
        await rebuild_player_ratings(sessionmaker)
        await session.commit()

        before = {
            (account, match): (round(mu, 6), round(sigma, 6))
            for account, match, mu, sigma in (
                await session.execute(
                    select(
                        PlayerRating.account_id,
                        PlayerRating.as_of_match_id,
                        PlayerRating.mu,
                        PlayerRating.sigma,
                    ).where(PlayerRating.as_of_match_id.in_([1000, 1001]))
                )
            ).all()
        }

        async with sessionmaker() as writer:
            await writer.execute(
                MatchPlayer.__table__.delete().where(MatchPlayer.match_id.in_([1002, 1003]))
            )
            await writer.execute(Match.__table__.delete().where(Match.match_id.in_([1002, 1003])))
            await writer.commit()

        await rebuild_player_ratings(sessionmaker)
        await session.commit()

        after = {
            (account, match): (round(mu, 6), round(sigma, 6))
            for account, match, mu, sigma in (
                await session.execute(
                    select(
                        PlayerRating.account_id,
                        PlayerRating.as_of_match_id,
                        PlayerRating.mu,
                        PlayerRating.sigma,
                    ).where(PlayerRating.as_of_match_id.in_([1000, 1001]))
                )
            ).all()
        }

        assert before == after


class TestUpdates:
    async def test_winners_rise_and_losers_fall(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        await seed_matches(session, [True, True])
        await rebuild_player_ratings(sessionmaker)
        await session.commit()

        latest = dict(
            (
                await session.execute(
                    select(PlayerRating.account_id, PlayerRating.mu).where(
                        PlayerRating.as_of_match_id == 1001
                    )
                )
            ).all()
        )
        assert latest[RADIANT_ACCOUNTS[0]] > ENV.mu
        assert latest[DIRE_ACCOUNTS[0]] < ENV.mu

    async def test_uncertainty_shrinks_with_games(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        await seed_matches(session, [True] * 5)
        await rebuild_player_ratings(sessionmaker)
        await session.commit()

        sigmas = [
            sigma
            for (sigma,) in (
                await session.execute(
                    select(PlayerRating.sigma)
                    .where(PlayerRating.account_id == RADIANT_ACCOUNTS[0])
                    .order_by(PlayerRating.as_of_time)
                )
            ).all()
        ]
        assert sigmas == sorted(sigmas, reverse=True)

    async def test_rebuild_is_deterministic(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """Rebuilt from scratch each time, so two runs over the same matches must agree -
        and the second must not double the rows."""
        await seed_matches(session, [True, False, True])

        first = await rebuild_player_ratings(sessionmaker)
        second = await rebuild_player_ratings(sessionmaker)

        assert first.ratings_written == second.ratings_written
        await session.commit()
        total = len((await session.execute(select(PlayerRating))).scalars().all())
        assert total == second.ratings_written

    async def test_incomplete_rosters_are_skipped(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """Most of our matches have no roster stored yet. Rating a half-present side would
        be worse than not rating it."""
        await seed_matches(session, [True])
        async with sessionmaker() as writer:
            await writer.execute(
                MatchPlayer.__table__.delete().where(MatchPlayer.account_id == DIRE_ACCOUNTS[0])
            )
            await writer.commit()

        report = await rebuild_player_ratings(sessionmaker)

        assert report.matches_processed == 0
        assert report.skipped == {"incomplete roster": 1}


class TestTeamAggregation:
    def test_sigma_combines_in_quadrature(self) -> None:
        """One unknown stand-in should not drag the whole side down to their uncertainty."""
        proven = [PlayerSkill(ENV.create_rating(mu=30, sigma=1.0), 50) for _ in range(4)]
        newcomer = PlayerSkill(ENV.create_rating(mu=25, sigma=8.0), 0)

        side = team_skill([*proven, newcomer])

        assert side.sigma < 8.0
        assert side.established == 4
        assert side.games == 0  # the least experienced member sets it

    def test_conservative_estimate_penalises_uncertainty(self) -> None:
        certain = team_skill([PlayerSkill(ENV.create_rating(mu=25, sigma=1.0), 100)] * 5)
        unknown = team_skill([PlayerSkill(ENV.create_rating(mu=25, sigma=8.0), 0)] * 5)

        assert certain.mu == unknown.mu
        assert certain.conservative > unknown.conservative

    def test_empty_side_falls_back_to_the_prior(self) -> None:
        side = team_skill([])
        assert side.mu == pytest.approx(ENV.mu)
        assert side.established == 0
