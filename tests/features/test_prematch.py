"""Pre-match features (spec sections 6.2, 6.3, 12).

Everything here is "state before this match", so the whole module stands or falls on one
ordering: ask the accumulators, write the row, and only then feed them the result. Each test
below is really a test of that ordering from a different angle.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.training import MatchPrematch, PlayerRating
from app.features.history import HeadToHead, HeroStats, TeamForm, decay
from app.features.prematch import (
    PREMATCH_FEATURES,
    prior_from_skill,
    rebuild_prematch,
)
from app.features.ratings import ENV, PlayerSkill, team_skill
from tests.features.test_ratings import (
    BASE,
    DIRE_ACCOUNTS,
    RADIANT_ACCOUNTS,
    seed_matches,
)


class TestHeroStats:
    def test_unseen_hero_sits_at_even(self) -> None:
        assert HeroStats().winrate(14) == pytest.approx(0.5)

    def test_a_short_streak_barely_moves_it(self) -> None:
        """Shrinkage is the point: a hero with two wins must not read as unbeatable."""
        stats = HeroStats()
        for _ in range(2):
            stats.observe((14,), (15,), radiant_win=True)
        assert 0.5 < stats.winrate(14) < 0.53

    def test_a_long_record_moves_it(self) -> None:
        stats = HeroStats()
        for _ in range(200):
            stats.observe((14,), (15,), radiant_win=True)
        assert stats.winrate(14) > 0.75
        assert stats.winrate(15) < 0.25

    def test_side_advantage_is_centred_on_zero(self) -> None:
        stats = HeroStats()
        assert stats.side_advantage((1, 2, 3), (4, 5, 6)) == pytest.approx(0.0)

    def test_missing_draft_gives_no_signal(self) -> None:
        assert HeroStats().side_advantage((), (4, 5)) == 0.0


class TestTeamForm:
    def test_unknown_team_is_even(self) -> None:
        assert TeamForm().form(None, BASE) == 0.5
        assert TeamForm().form(999, BASE) == 0.5

    def test_recent_results_outweigh_old_ones(self) -> None:
        form = TeamForm()
        form.observe(1, BASE - timedelta(days=90), won=True)
        form.observe(1, BASE - timedelta(days=1), won=False)
        # The loss is a day old and the win three months; form should read badly.
        assert form.form(1, BASE) < 0.3

    def test_results_beyond_the_window_are_dropped(self) -> None:
        form = TeamForm()
        form.observe(1, BASE - timedelta(days=200), won=True)
        assert form.form(1, BASE) == 0.5

    def test_rest_days_and_recent_load(self) -> None:
        form = TeamForm()
        form.observe(1, BASE - timedelta(hours=3), won=True)
        form.observe(1, BASE - timedelta(hours=5), won=True)
        assert form.rest_days(1, BASE) == pytest.approx(0.125, abs=0.01)
        assert form.maps_since(1, BASE, timedelta(days=1)) == 2

    def test_never_seen_team_has_no_rest_figure(self) -> None:
        """None, not zero: never having seen a team is not the same as them being fresh."""
        assert TeamForm().rest_days(1, BASE) is None


class TestHeadToHead:
    def test_teams_that_never_met_are_even(self) -> None:
        assert HeadToHead().advantage(1, 2, BASE) == 0.5

    def test_perspective_flips_between_the_two_sides(self) -> None:
        h2h = HeadToHead()
        h2h.observe(1, 2, BASE - timedelta(days=5), team_won=True)
        assert h2h.advantage(1, 2, BASE) > 0.5
        assert h2h.advantage(2, 1, BASE) < 0.5

    def test_order_of_arguments_does_not_matter_for_storage(self) -> None:
        left = HeadToHead()
        left.observe(2, 1, BASE - timedelta(days=5), team_won=False)
        right = HeadToHead()
        right.observe(1, 2, BASE - timedelta(days=5), team_won=True)
        assert left.advantage(1, 2, BASE) == pytest.approx(right.advantage(1, 2, BASE))

    def test_meetings_beyond_six_months_are_forgotten(self) -> None:
        h2h = HeadToHead()
        h2h.observe(1, 2, BASE - timedelta(days=200), team_won=True)
        assert h2h.advantage(1, 2, BASE) == 0.5


class TestDecay:
    def test_half_life(self) -> None:
        assert decay(timedelta(days=30)) == pytest.approx(0.5)
        assert decay(timedelta(days=0)) == pytest.approx(1.0)


class TestPrior:
    def test_equal_sides_favour_radiant_slightly(self) -> None:
        """Radiant wins a little more often at every level of play."""
        even = team_skill([PlayerSkill(ENV.create_rating(), 50)] * 5)
        prior = prior_from_skill(even, even)
        assert 0.5 < prior < 0.55

    def test_a_stronger_side_gets_a_higher_prior(self) -> None:
        strong = team_skill([PlayerSkill(ENV.create_rating(mu=35, sigma=1.0), 100)] * 5)
        weak = team_skill([PlayerSkill(ENV.create_rating(mu=20, sigma=1.0), 100)] * 5)
        assert prior_from_skill(strong, weak) > 0.8
        assert prior_from_skill(weak, strong) < 0.2

    def test_uncertainty_pulls_the_prior_back(self) -> None:
        """Same means, but one side is unproven - the prior should hedge."""
        certain = team_skill([PlayerSkill(ENV.create_rating(mu=30, sigma=1.0), 100)] * 5)
        unproven = team_skill([PlayerSkill(ENV.create_rating(mu=30, sigma=8.0), 0)] * 5)
        assert prior_from_skill(certain, unproven) > 0.5


class TestSweep:
    async def test_writes_a_row_per_match_with_every_feature(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        await seed_matches(session, [True, False, True])
        report = await rebuild_prematch(sessionmaker)
        await session.commit()

        assert report.rows_written == 3
        rows = (await session.execute(select(MatchPrematch))).scalars().all()
        assert len(rows) == 3
        for row in rows:
            assert set(row.features) == set(PREMATCH_FEATURES)
            assert 0.0 < row.prematch_prior < 1.0

    async def test_the_first_match_knows_nothing(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """Every accumulator is empty, so every difference must be zero and the prior must
        be the radiant bias alone."""
        await seed_matches(session, [True, True])
        await rebuild_prematch(sessionmaker)
        await session.commit()

        first = (
            await session.execute(select(MatchPrematch).where(MatchPrematch.match_id == 1000))
        ).scalar_one()

        assert first.features["skill_diff"] == pytest.approx(0.0)
        assert first.features["form_diff"] == pytest.approx(0.0)
        assert first.features["h2h_advantage"] == pytest.approx(0.0)
        assert first.features["draft_advantage"] == pytest.approx(0.0)
        assert first.features["rest_days_diff"] == pytest.approx(0.0)
        assert 0.5 < first.prematch_prior < 0.55

    async def test_a_winning_streak_moves_the_later_features(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        await seed_matches(session, [True] * 4)
        await rebuild_prematch(sessionmaker)
        await session.commit()

        rows = {
            row.match_id: row
            for row in (await session.execute(select(MatchPrematch))).scalars().all()
        }
        assert rows[1003].features["skill_diff"] > rows[1000].features["skill_diff"]
        assert rows[1003].features["form_diff"] > 0.5
        assert rows[1003].features["h2h_advantage"] > 0.0
        assert rows[1003].prematch_prior > rows[1000].prematch_prior

    async def test_no_match_contributes_to_its_own_features(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """The leakage check, in the form that matters here: dropping later matches must
        leave the earlier feature rows byte-identical."""
        await seed_matches(session, [True, False, True, False])
        await rebuild_prematch(sessionmaker)
        await session.commit()

        def snapshot(rows: list[MatchPrematch]) -> dict[int, str]:
            return {
                r.match_id: repr(sorted((k, round(v, 9)) for k, v in r.features.items()))
                for r in rows
                if r.match_id in (1000, 1001)
            }

        before = snapshot((await session.execute(select(MatchPrematch))).scalars().all())

        async with sessionmaker() as writer:
            from app.db.models.matches import Match, MatchPlayer

            await writer.execute(
                MatchPlayer.__table__.delete().where(MatchPlayer.match_id.in_([1002, 1003]))
            )
            await writer.execute(Match.__table__.delete().where(Match.match_id.in_([1002, 1003])))
            await writer.commit()

        await rebuild_prematch(sessionmaker)
        await session.commit()
        after = snapshot((await session.execute(select(MatchPrematch))).scalars().all())

        assert before == after

    async def test_ratings_are_written_by_the_same_sweep(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """One walk over history, so the ratings and the features cannot drift apart."""
        await seed_matches(session, [True, False])
        await rebuild_prematch(sessionmaker)
        await session.commit()

        ratings = (await session.execute(select(PlayerRating))).scalars().all()
        assert len(ratings) == 20
        firsts = [r for r in ratings if r.as_of_match_id == 1000]
        assert all(r.games == 0 and r.mu == pytest.approx(ENV.mu) for r in firsts)

    async def test_rebuild_is_idempotent(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        await seed_matches(session, [True, False, True])
        first = await rebuild_prematch(sessionmaker)
        second = await rebuild_prematch(sessionmaker)

        assert first.rows_written == second.rows_written
        await session.commit()
        assert len((await session.execute(select(MatchPrematch))).scalars().all()) == 3

    async def test_incomplete_rosters_are_skipped(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        await seed_matches(session, [True])
        async with sessionmaker() as writer:
            from app.db.models.matches import MatchPlayer

            await writer.execute(
                MatchPlayer.__table__.delete().where(MatchPlayer.account_id == DIRE_ACCOUNTS[0])
            )
            await writer.commit()

        report = await rebuild_prematch(sessionmaker)

        assert report.rows_written == 0
        assert report.skipped == {"incomplete roster": 1}


def test_accounts_fixture_is_ten_players() -> None:
    assert len(set(RADIANT_ACCOUNTS + DIRE_ACCOUNTS)) == 10
    assert datetime.now(UTC) > BASE
