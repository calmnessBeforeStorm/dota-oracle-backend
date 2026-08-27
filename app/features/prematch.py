"""Pre-match features, in one chronological sweep (spec sections 6.2, 6.3, phase 3).

Player ratings, hero winrates, team form and head-to-head are all "state before this match",
and they all update from the same result. Computing them in separate passes would mean four
walks over history and four chances for one of them to drift out of step with the others.

The order inside the loop is the whole safety argument:

    1. ask every accumulator what it knows          <- becomes the feature row
    2. write the row
    3. feed them the result of this match           <- affects only later matches

Read it in any other order and each match predicts itself.

The output is one row per map in `match_prematch`, plus the per-player ratings that back it.
`prematch_prior` is a logistic on the skill difference: the simplest honest pre-match model,
and baseline number four from spec section 7.3, which the real model has to beat.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.db.models.matches import Match, MatchPlayer
from app.db.models.training import MatchPrematch, PlayerRating
from app.features.history import HeadToHead, HeroStats, TeamForm
from app.features.ratings import ENV, PlayerSkill, TeamSkill, team_skill
from app.ingestion.repository import utcnow

log = get_logger(__name__)

#: Scale that turns a TrueSkill difference into a probability. Fitted by eye rather than by
#: optimisation, which is fine for a baseline whose job is to be beaten.
SKILL_TO_LOGIT = 0.12

#: Radiant wins slightly more often than dire, consistently, across every level of play.
RADIANT_BIAS = 0.055

DAY = timedelta(days=1)

#: Feature names this module produces, in a fixed order.
PREMATCH_FEATURES: tuple[str, ...] = (
    "skill_diff",
    "skill_sigma_sum",
    "established_diff",
    "form_diff",
    "h2h_advantage",
    "draft_advantage",
    "rest_days_diff",
    "maps_last_24h_diff",
)


@dataclass
class PrematchReport:
    matches_processed: int = 0
    rows_written: int = 0
    ratings_written: int = 0
    skipped: dict[str, int] = field(default_factory=dict)

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1

    def as_log_fields(self) -> dict[str, Any]:
        return {
            "matches_processed": self.matches_processed,
            "rows_written": self.rows_written,
            "ratings_written": self.ratings_written,
            "skipped": self.skipped,
        }


def prior_from_skill(radiant: TeamSkill, dire: TeamSkill) -> float:
    """Pre-match win probability for radiant, from the two sides' conservative skill."""
    logit = RADIANT_BIAS + SKILL_TO_LOGIT * (radiant.conservative - dire.conservative)
    return 1.0 / (1.0 + math.exp(-max(min(logit, 20.0), -20.0)))


@dataclass
class SweepState:
    """Everything the sweep carries forward. Nothing here may be seeded from the future."""

    skills: dict[int, PlayerSkill] = field(default_factory=dict)
    heroes: HeroStats = field(default_factory=HeroStats)
    form: TeamForm = field(default_factory=TeamForm)
    h2h: HeadToHead = field(default_factory=HeadToHead)


@dataclass(frozen=True)
class MatchRoster:
    radiant_accounts: list[int]
    dire_accounts: list[int]
    radiant_heroes: tuple[int, ...]
    dire_heroes: tuple[int, ...]

    @property
    def is_complete(self) -> bool:
        return len(self.radiant_accounts) == 5 and len(self.dire_accounts) == 5


def build_features(
    state: SweepState,
    roster: MatchRoster,
    radiant_team_id: int | None,
    dire_team_id: int | None,
    played_at: datetime,
) -> tuple[dict[str, float], float, TeamSkill, TeamSkill]:
    """The pre-match view, from what the accumulators know so far."""
    radiant_skill = team_skill(
        [
            state.skills.setdefault(a, PlayerSkill(ENV.create_rating()))
            for a in roster.radiant_accounts
        ]
    )
    dire_skill = team_skill(
        [state.skills.setdefault(a, PlayerSkill(ENV.create_rating())) for a in roster.dire_accounts]
    )

    radiant_rest = state.form.rest_days(radiant_team_id, played_at)
    dire_rest = state.form.rest_days(dire_team_id, played_at)

    features = {
        "skill_diff": radiant_skill.conservative - dire_skill.conservative,
        # High combined uncertainty is itself informative: it says the prior is thin.
        "skill_sigma_sum": radiant_skill.sigma + dire_skill.sigma,
        "established_diff": float(radiant_skill.established - dire_skill.established),
        "form_diff": state.form.form(radiant_team_id, played_at)
        - state.form.form(dire_team_id, played_at),
        "h2h_advantage": state.h2h.advantage(radiant_team_id, dire_team_id, played_at) - 0.5,
        "draft_advantage": state.heroes.side_advantage(roster.radiant_heroes, roster.dire_heroes),
        # Unknown rest is encoded as zero difference rather than as a large one: never
        # having seen a team is not the same as them being well rested.
        "rest_days_diff": (
            0.0 if radiant_rest is None or dire_rest is None else radiant_rest - dire_rest
        ),
        "maps_last_24h_diff": float(
            state.form.maps_since(radiant_team_id, played_at, DAY)
            - state.form.maps_since(dire_team_id, played_at, DAY)
        ),
    }
    return features, prior_from_skill(radiant_skill, dire_skill), radiant_skill, dire_skill


def observe(
    state: SweepState,
    roster: MatchRoster,
    radiant_team_id: int | None,
    dire_team_id: int | None,
    played_at: datetime,
    radiant_win: bool,
) -> None:
    """Feed the result forward. Called only after the feature row has been built."""
    updated_radiant, updated_dire = ENV.rate(
        [
            [state.skills[a].rating for a in roster.radiant_accounts],
            [state.skills[a].rating for a in roster.dire_accounts],
        ],
        ranks=[0, 1] if radiant_win else [1, 0],
    )
    for account_id, rating in zip(
        roster.radiant_accounts + roster.dire_accounts,
        list(updated_radiant) + list(updated_dire),
        strict=True,
    ):
        state.skills[account_id] = PlayerSkill(rating, state.skills[account_id].games + 1)

    state.heroes.observe(roster.radiant_heroes, roster.dire_heroes, radiant_win)
    state.form.observe(radiant_team_id, played_at, radiant_win)
    state.form.observe(dire_team_id, played_at, not radiant_win)
    state.h2h.observe(radiant_team_id, dire_team_id, played_at, radiant_win)


async def rebuild_prematch(
    session_factory: async_sessionmaker[AsyncSession],
    batch_size: int = 500,
) -> PrematchReport:
    """Replay every finished match in order, recording what was knowable before each."""
    report = PrematchReport()
    state = SweepState()

    async with session_factory() as session:
        # A rebuild must not leave rows from an ordering that no longer holds.
        await session.execute(delete(MatchPrematch))
        await session.execute(delete(PlayerRating))
        await session.commit()

    offset = 0
    while True:
        async with session_factory() as session:
            match_rows = (
                await session.execute(
                    select(
                        Match.match_id,
                        Match.start_time,
                        Match.radiant_win,
                        Match.radiant_team_id,
                        Match.dire_team_id,
                    )
                    .where(Match.radiant_win.is_not(None), Match.start_time.is_not(None))
                    .order_by(Match.start_time, Match.match_id)
                    .offset(offset)
                    .limit(batch_size)
                )
            ).all()
            if not match_rows:
                break

            await _sweep_batch(session, match_rows, state, report)
            await session.commit()

        offset += len(match_rows)

    log.info("prematch.rebuilt", **report.as_log_fields())
    return report


async def _rosters_for(session: AsyncSession, match_ids: list[int]) -> dict[int, MatchRoster]:
    rows = (
        await session.execute(
            select(
                MatchPlayer.match_id,
                MatchPlayer.account_id,
                MatchPlayer.hero_id,
                MatchPlayer.is_radiant,
            ).where(MatchPlayer.match_id.in_(match_ids))
        )
    ).all()

    collected: dict[int, dict[str, list[Any]]] = {}
    for match_id, account_id, hero_id, is_radiant in rows:
        bucket = collected.setdefault(
            int(match_id),
            {"radiant_accounts": [], "dire_accounts": [], "radiant_heroes": [], "dire_heroes": []},
        )
        side = "radiant" if is_radiant else "dire"
        if account_id is not None:
            bucket[f"{side}_accounts"].append(int(account_id))
        if hero_id is not None:
            bucket[f"{side}_heroes"].append(int(hero_id))

    return {
        match_id: MatchRoster(
            radiant_accounts=parts["radiant_accounts"],
            dire_accounts=parts["dire_accounts"],
            radiant_heroes=tuple(parts["radiant_heroes"]),
            dire_heroes=tuple(parts["dire_heroes"]),
        )
        for match_id, parts in collected.items()
    }


async def _sweep_batch(
    session: AsyncSession,
    match_rows: Sequence[Any],
    state: SweepState,
    report: PrematchReport,
) -> None:
    rosters = await _rosters_for(session, [int(row[0]) for row in match_rows])
    now = utcnow()

    for match_id, start_time, radiant_win, radiant_team_id, dire_team_id in match_rows:
        roster = rosters.get(int(match_id))
        if roster is None or not roster.is_complete:
            # Without both full sides the rating update would be lopsided, and the draft
            # feature would compare five heroes against however many were recorded.
            report.skip("incomplete roster")
            continue

        # 1. what was knowable before this match
        features, prior, _, _ = build_features(
            state, roster, radiant_team_id, dire_team_id, start_time
        )

        # 2. write it down, ratings included
        session.add(
            MatchPrematch(
                match_id=int(match_id),
                features=features,
                prematch_prior=prior,
                computed_at=now,
            )
        )
        for account_id in roster.radiant_accounts + roster.dire_accounts:
            skill = state.skills[account_id]
            session.add(
                PlayerRating(
                    account_id=account_id,
                    as_of_match_id=int(match_id),
                    as_of_time=start_time,
                    mu=skill.rating.mu,
                    sigma=skill.rating.sigma,
                    games=skill.games,
                )
            )
        report.rows_written += 1
        report.ratings_written += 10

        # 3. only now does this match exist as far as the accumulators are concerned
        observe(state, roster, radiant_team_id, dire_team_id, start_time, bool(radiant_win))
        report.matches_processed += 1
