"""The pre-match block the live path can supply, read straight from stored ratings.

Nine of the twenty-eight features came from the pre-match sweep and the poller passed none
of them, so at serving time each one arrived as the builder's default - the same number in
every match - while training had seen real values. Measured 10.09.2026 on
`lgbm-20260901-102407`: 1575 of its 2190 splits stood on that block, `prematch_prior`
arrived as 0.5 (a value present in exactly 0 of 242 295 training rows) and
`skill_sigma_sum` as 0.0, below its training minimum of 1.2356 - a number the model had
never seen at all.

The sweep's accumulators (form, head-to-head, hero stats) replay the whole archive in order
and cannot be rebuilt on a 20-second tick. The skill half needs no replay: `player_ratings`
is already written point-in-time, one row per account per match, so the newest row at or
before kickoff is exactly what was knowable before this game. That covers the four features
the model leant on hardest, prior included.

The remaining four stay out of the served set rather than being filled in. A constant
dressed as a feature is the defect this module exists to fix, not a smaller version of it.
"""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.training import PlayerRating
from app.features.prematch import prior_from_skill
from app.features.ratings import ENV, PlayerSkill, team_skill

#: What this module fills. Deliberately smaller than `PREMATCH_FEATURE_NAMES`.
LIVE_PREMATCH_FEATURES: tuple[str, ...] = (
    "skill_diff",
    "skill_sigma_sum",
    "established_diff",
)


async def _latest_skills(
    session: AsyncSession, accounts: list[int], at: datetime
) -> dict[int, PlayerSkill]:
    """Each account's newest rating at or before `at`.

    The time bound is the point-in-time invariant, not caution: a rating row written after
    this game started already knows how it went, and feeding it back in would be the model
    grading its own homework.
    """
    wanted = {int(account) for account in accounts if account}
    if not wanted:
        return {}

    newest = (
        select(PlayerRating.account_id, PlayerRating.mu, PlayerRating.sigma, PlayerRating.games)
        .where(PlayerRating.account_id.in_(wanted), PlayerRating.as_of_time <= at)
        .distinct(PlayerRating.account_id)
        .order_by(PlayerRating.account_id, PlayerRating.as_of_time.desc())
    )
    rows = (await session.execute(newest)).all()
    return {
        int(account_id): PlayerSkill(ENV.create_rating(mu=mu, sigma=sigma), games=int(games))
        for account_id, mu, sigma, games in rows
    }


def _side_skill(accounts: list[int], known: dict[int, PlayerSkill]) -> list[PlayerSkill]:
    """Ratings for one side, defaulting an unseen account rather than dropping it.

    A debutant gets the environment's starting rating, which carries a deliberately large
    sigma. That is the honest encoding: it says we do not know, where dropping the player
    would quietly make the side look like a four-man team and substituting the mean would
    claim knowledge we do not have.
    """
    return [
        known.get(int(account), PlayerSkill(ENV.create_rating(), games=0)) for account in accounts
    ]


async def live_prematch(
    session: AsyncSession,
    *,
    radiant_accounts: list[int],
    dire_accounts: list[int],
    at: datetime,
) -> tuple[dict[str, float], float]:
    """Skill features and the prior for a game that is starting or under way."""
    known = await _latest_skills(session, radiant_accounts + dire_accounts, at)
    radiant = team_skill(_side_skill(radiant_accounts, known))
    dire = team_skill(_side_skill(dire_accounts, known))

    features = {
        "skill_diff": radiant.conservative - dire.conservative,
        # High combined uncertainty is itself informative: it says the prior is thin.
        "skill_sigma_sum": radiant.sigma + dire.sigma,
        "established_diff": float(radiant.established - dire.established),
    }
    return features, prior_from_skill(radiant, dire)
