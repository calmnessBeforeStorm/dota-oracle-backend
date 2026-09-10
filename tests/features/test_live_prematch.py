"""The pre-match block the poller can supply today (spec section 6.4).

Nine of the model's twenty-eight features came from the pre-match sweep, and the live path
passed none of them. At serving time each arrived as the builder's default - the same number
in every match - while training had seen real values. Measured 10.09.2026 on
`lgbm-20260901-102407`: 1575 of its 2190 splits stood on that block, `prematch_prior` arrived
as 0.5 (a value present in exactly 0 of 242 295 training rows) and `skill_sigma_sum` as 0.0,
below its training minimum of 1.2356 - a number the model had never seen at all.

The sweep's accumulators cannot be rebuilt per tick, but they do not have to be for the
skill half: `player_ratings` is already written point-in-time by the sweep, so the latest
row at or before now is exactly what was knowable before this game. That covers
`skill_diff`, `skill_sigma_sum`, `established_diff` and the prior - the four the model leant
on hardest.

The other four (`form_diff`, `h2h_advantage`, `draft_advantage`, `maps_last_24h_diff`,
`rest_days_diff`) still need the accumulators and stay out of the served set until they can
be supplied honestly. Filling them with a constant is the defect this fixes, not a smaller
version of it.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.training import PlayerRating
from app.features.prematch_live import live_prematch

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


async def rate(
    session: AsyncSession,
    account_id: int,
    *,
    mu: float,
    sigma: float,
    games: int,
    minutes_ago: int,
) -> None:
    session.add(
        PlayerRating(
            account_id=account_id,
            as_of_match_id=None,
            as_of_time=NOW - timedelta(minutes=minutes_ago),
            mu=mu,
            sigma=sigma,
            games=games,
        )
    )
    await session.commit()


class TestLivePrematch:
    async def test_a_stronger_side_gets_a_positive_skill_diff(self, session: AsyncSession) -> None:
        for account_id in range(1, 6):
            await rate(session, account_id, mu=35.0, sigma=1.0, games=200, minutes_ago=60)
        for account_id in range(6, 11):
            await rate(session, account_id, mu=20.0, sigma=1.0, games=200, minutes_ago=60)

        features, prior = await live_prematch(
            session, radiant_accounts=list(range(1, 6)), dire_accounts=list(range(6, 11)), at=NOW
        )

        assert features["skill_diff"] > 0
        assert prior > 0.5

    async def test_the_newest_rating_at_or_before_now_wins(self, session: AsyncSession) -> None:
        # Point-in-time is the whole invariant: a rating written after this game started
        # already knows how it went.
        for account_id in range(1, 11):
            await rate(session, account_id, mu=25.0, sigma=2.0, games=50, minutes_ago=120)
        await rate(session, 1, mu=45.0, sigma=1.0, games=300, minutes_ago=30)
        await rate(session, 2, mu=99.0, sigma=1.0, games=999, minutes_ago=-30)

        features, _ = await live_prematch(
            session, radiant_accounts=list(range(1, 6)), dire_accounts=list(range(6, 11)), at=NOW
        )

        # Account 1's newer row counts, account 2's future row does not.
        assert features["skill_diff"] == pytest.approx(4.0, abs=0.5)

    async def test_an_unrated_account_does_not_fabricate_skill(self, session: AsyncSession) -> None:
        # A debutant has no row. Substituting the mean would claim we know something about
        # them; the default rating carries its own large sigma, which says we do not.
        features, prior = await live_prematch(
            session, radiant_accounts=list(range(1, 6)), dire_accounts=list(range(6, 11)), at=NOW
        )

        assert features["skill_diff"] == pytest.approx(0.0)
        assert features["skill_sigma_sum"] > 0
        # Exactly 0.5 was the fingerprint of the constant being removed here - a value
        # present in none of the training rows. Even two identical line-ups do not produce
        # it, because radiant carries a measured historical edge.
        assert prior != 0.5
        assert 0.5 < prior < 0.55

    def test_it_returns_only_what_it_can_actually_supply(self) -> None:
        # The four that need the sweep's accumulators must not appear with a filled-in
        # value: a constant dressed as a feature is the defect being fixed here.
        from app.features.prematch_live import LIVE_PREMATCH_FEATURES

        assert set(LIVE_PREMATCH_FEATURES) == {
            "skill_diff",
            "skill_sigma_sum",
            "established_diff",
        }


class TestServedSet:
    """Supplied is not the same as used, and the difference is a measurement.

    Once the poller could send the skill block, the obvious next step was to train on it.
    Measured 10.09.2026 on the same split: 19 features gave log loss 0.5246 and passed the
    gate, 23 gave 0.5276 and failed it on minutes 5-9 and 10-14 - the early game, which is
    exactly where a prior is supposed to help. So the block stays out of the vector.

    Supplying it is still worth doing: the values land in `predictions.features`, so the
    next attempt can argue from what was actually served rather than guessing again.
    """

    def test_the_block_stays_out_of_the_vector(self) -> None:
        from app.features.live import SERVED_FEATURES

        for name in ("skill_diff", "skill_sigma_sum", "established_diff", "prematch_prior"):
            assert name not in SERVED_FEATURES

    def test_the_poller_supplies_it_regardless(self) -> None:
        from app.features.live import SUPPLIED_BUT_UNUSED

        assert set(SUPPLIED_BUT_UNUSED) == {
            "skill_diff",
            "skill_sigma_sum",
            "established_diff",
            "prematch_prior",
        }
