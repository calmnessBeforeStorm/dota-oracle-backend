"""Point-in-time player ratings (spec sections 4.3, 6.2, phase 3).

Ratings are per `account_id`, not per team. A team as an organisation is a fiction across
roster shuffles; five specific accounts with a history are not (spec section 6.2). Team
strength is aggregated from the five, uncertainty included, which is why TrueSkill is used
rather than Elo - the sigma is a feature, not an implementation detail.

The single rule that makes this table usable at all: **the rating stored against a match is
the rating as it stood BEFORE that match was played**. Everything is processed strictly in
chronological order, and the update from a match is applied only after its row is written.
Compute the ratings any other way and every pre-match feature quietly knows the result of
the game it is describing.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import trueskill
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.db.models.matches import Match, MatchPlayer
from app.db.models.training import PlayerRating

log = get_logger(__name__)

#: Draws do not exist at the map level in Dota 2 - there is always a winner (spec section 1.4).
ENV = trueskill.TrueSkill(draw_probability=0.0)

#: Below this a player's rating is mostly prior, not evidence. Kept as a feature rather than
#: used to filter: spec section 5.3 filters on metadata, and this is one.
ESTABLISHED_AFTER_GAMES = 20


@dataclass
class RatingsReport:
    matches_processed: int = 0
    ratings_written: int = 0
    players: int = 0
    skipped: dict[str, int] = field(default_factory=dict)

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1

    def as_log_fields(self) -> dict[str, Any]:
        return {
            "matches_processed": self.matches_processed,
            "ratings_written": self.ratings_written,
            "players": self.players,
            "skipped": self.skipped,
        }


@dataclass
class PlayerSkill:
    rating: trueskill.Rating
    games: int = 0

    @property
    def is_established(self) -> bool:
        return self.games >= ESTABLISHED_AFTER_GAMES


@dataclass(frozen=True)
class TeamSkill:
    """A side's strength, aggregated from the five accounts that played it."""

    mu: float
    sigma: float
    games: int
    established: int

    @property
    def conservative(self) -> float:
        """Skill discounted by uncertainty - the usual "how good are they, really" number.

        A stack of unknowns and a stack of proven players can share a mean; they should not
        share a rating.
        """
        return self.mu - 3 * self.sigma


def team_skill(skills: list[PlayerSkill]) -> TeamSkill:
    """Aggregate five players into one side.

    Sigmas add in quadrature because the players are independent, so a single unknown
    stand-in does not drag the whole side's certainty down to their level.
    """
    if not skills:
        return TeamSkill(mu=ENV.mu, sigma=ENV.sigma, games=0, established=0)

    mu = sum(s.rating.mu for s in skills) / len(skills)
    sigma = (sum(s.rating.sigma**2 for s in skills) ** 0.5) / len(skills)
    return TeamSkill(
        mu=mu,
        sigma=sigma,
        games=min(s.games for s in skills),
        established=sum(1 for s in skills if s.is_established),
    )


async def rebuild_player_ratings(
    session_factory: async_sessionmaker[AsyncSession],
    batch_size: int = 500,
) -> RatingsReport:
    """Replay every match in chronological order, recording each player's prior rating.

    A full rebuild rather than an incremental update: ratings depend on the order matches
    were played, so a match arriving late - a backfill reaching further into history -
    invalidates everything after it. Rebuilding is cheap and always correct.
    """
    report = RatingsReport()
    skills: dict[int, PlayerSkill] = {}

    async with session_factory() as session:
        # Wiped rather than upserted: a rebuild must not leave rows from an ordering that
        # no longer holds.
        await session.execute(delete(PlayerRating))
        await session.commit()

    offset = 0
    while True:
        async with session_factory() as session:
            match_rows = (
                await session.execute(
                    select(Match.match_id, Match.start_time, Match.radiant_win)
                    .where(
                        Match.radiant_win.is_not(None),
                        Match.start_time.is_not(None),
                    )
                    .order_by(Match.start_time, Match.match_id)
                    .offset(offset)
                    .limit(batch_size)
                )
            ).all()

            if not match_rows:
                break

            rows_to_write = await _process_matches(session, match_rows, skills, report)
            if rows_to_write:
                session.add_all(rows_to_write)
                report.ratings_written += len(rows_to_write)
            await session.commit()

        offset += len(match_rows)

    report.players = len(skills)
    log.info("ratings.rebuilt", **report.as_log_fields())
    return report


async def _process_matches(
    session: AsyncSession,
    match_rows: Sequence[Any],
    skills: dict[int, PlayerSkill],
    report: RatingsReport,
) -> list[PlayerRating]:
    match_ids = [int(row[0]) for row in match_rows]
    players_by_match: dict[int, list[tuple[int, bool]]] = {}
    for match_id, account_id, is_radiant in (
        await session.execute(
            select(MatchPlayer.match_id, MatchPlayer.account_id, MatchPlayer.is_radiant).where(
                MatchPlayer.match_id.in_(match_ids), MatchPlayer.account_id.is_not(None)
            )
        )
    ).all():
        players_by_match.setdefault(int(match_id), []).append((int(account_id), bool(is_radiant)))

    written: list[PlayerRating] = []
    for match_id, start_time, radiant_win in match_rows:
        roster = players_by_match.get(int(match_id), [])
        if len(roster) < 10:
            # Without both full sides the update would be lopsided; the match is skipped
            # rather than applied to whoever happens to be present.
            report.skip("incomplete roster")
            continue

        written.extend(
            _record_and_update(int(match_id), start_time, bool(radiant_win), roster, skills)
        )
        report.matches_processed += 1

    return written


def _record_and_update(
    match_id: int,
    start_time: datetime,
    radiant_win: bool,
    roster: list[tuple[int, bool]],
    skills: dict[int, PlayerSkill],
) -> list[PlayerRating]:
    """Write the pre-match ratings, then apply the result. Order matters, entirely."""
    rows = [
        PlayerRating(
            account_id=account_id,
            as_of_match_id=match_id,
            as_of_time=start_time,
            mu=skills.setdefault(account_id, PlayerSkill(ENV.create_rating())).rating.mu,
            sigma=skills[account_id].rating.sigma,
            games=skills[account_id].games,
        )
        for account_id, _ in roster
    ]

    radiant = [account_id for account_id, is_radiant in roster if is_radiant]
    dire = [account_id for account_id, is_radiant in roster if not is_radiant]

    updated_radiant, updated_dire = ENV.rate(
        [
            [skills[a].rating for a in radiant],
            [skills[a].rating for a in dire],
        ],
        ranks=[0, 1] if radiant_win else [1, 0],
    )

    for account_id, rating in zip(
        radiant + dire, list(updated_radiant) + list(updated_dire), strict=True
    ):
        skills[account_id] = PlayerSkill(rating, skills[account_id].games + 1)

    return rows


async def skills_before(session: AsyncSession, match_id: int) -> tuple[TeamSkill, TeamSkill] | None:
    """Both sides' strength as it stood before a match, for pre-match features."""
    rows = (
        await session.execute(
            select(
                PlayerRating.account_id,
                PlayerRating.mu,
                PlayerRating.sigma,
                PlayerRating.games,
                MatchPlayer.is_radiant,
            )
            .join(
                MatchPlayer,
                (MatchPlayer.account_id == PlayerRating.account_id)
                & (MatchPlayer.match_id == PlayerRating.as_of_match_id),
            )
            .where(PlayerRating.as_of_match_id == match_id)
        )
    ).all()
    if not rows:
        return None

    sides: dict[bool, list[PlayerSkill]] = {True: [], False: []}
    for _, mu, sigma, games, is_radiant in rows:
        sides[bool(is_radiant)].append(
            PlayerSkill(trueskill.Rating(mu=float(mu), sigma=float(sigma)), int(games))
        )
    return team_skill(sides[True]), team_skill(sides[False])
