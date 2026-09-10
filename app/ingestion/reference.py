"""Reference data that is not per-match: heroes and pro players (spec section 2.2/A3).

Both are small, change rarely, and cost one call each. They exist because `match_drafts` and
`match_players` store bare ids: without them a match card can only show numbers, which is
what F2 is for.

Kept apart from `normalize.py` on purpose. That module reads only from `raw_matches` and
never touches the network; these two do the opposite, and mixing the rules is how a rebuild
starts spending quota.
"""

from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.db.models.reference import Hero, League, Player
from app.ingestion.repository import utcnow

log = get_logger(__name__)


class HeroSource(Protocol):
    async def heroes(self) -> dict[str, Any]: ...


class ProPlayerSource(Protocol):
    async def pro_players(self) -> list[dict[str, Any]]: ...


class LeagueSource(Protocol):
    async def leagues(self) -> list[dict[str, Any]]: ...


@dataclass
class ReferenceReport:
    heroes: int = 0
    players: int = 0
    leagues: int = 0


def parse_heroes(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """`/constants/heroes` is keyed by id-as-string; the id is also inside each value."""
    rows: list[dict[str, Any]] = []
    now = utcnow()
    for hero in payload.values():
        hero_id = hero.get("id")
        if hero_id is None or not hero.get("name"):
            continue
        rows.append(
            {
                "hero_id": int(hero_id),
                "name": str(hero["name"])[:64],
                # Falls back to the internal name rather than to a placeholder: a hero with
                # no display name is a data problem worth seeing, not one worth hiding.
                "localized_name": str(hero.get("localized_name") or hero["name"])[:64],
                "primary_attr": (hero.get("primary_attr") or None),
                "attack_type": (hero.get("attack_type") or None),
                "roles": list(hero.get("roles") or []),
                "image_path": (hero.get("img") or None),
                "created_at": now,
                "updated_at": now,
            }
        )
    return rows


def parse_pro_players(payload: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`/proPlayers` gives `account_id` and the handle the player is known by.

    `name` is the pro handle and `personaname` the Steam display name; the first is what a
    match card should show, and it is null for players without one.
    """
    rows: list[dict[str, Any]] = []
    now = utcnow()
    seen: set[int] = set()
    for player in payload:
        account_id = player.get("account_id")
        if account_id is None or int(account_id) in seen:
            continue
        seen.add(int(account_id))
        rows.append(
            {
                "account_id": int(account_id),
                "name": (player.get("name") or player.get("personaname") or None),
                "country": (player.get("loccountrycode") or None),
                "fantasy_role": player.get("fantasy_role"),
                "created_at": now,
                "updated_at": now,
            }
        )
    return rows


async def _upsert(session: AsyncSession, model: Any, rows: list[dict[str, Any]], key: str) -> int:
    if not rows:
        return 0
    statement = insert(model).values(rows)
    updatable = sorted(set(rows[0]) - {key, "created_at"})
    statement = statement.on_conflict_do_update(
        index_elements=[key],
        set_={name: getattr(statement.excluded, name) for name in updatable},
    )
    await session.execute(statement)
    return len(rows)


async def refresh_heroes(
    client: HeroSource, session_factory: async_sessionmaker[AsyncSession]
) -> int:
    rows = parse_heroes(await client.heroes())
    async with session_factory() as session:
        written = await _upsert(session, Hero, rows, "hero_id")
        await session.commit()
    log.info("reference.heroes", written=written)
    return written


async def refresh_pro_players(
    client: ProPlayerSource, session_factory: async_sessionmaker[AsyncSession]
) -> int:
    rows = parse_pro_players(await client.pro_players())
    async with session_factory() as session:
        written = await _upsert(session, Player, rows, "account_id")
        await session.commit()
    log.info("reference.pro_players", written=written)
    return written


def parse_leagues(payload: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`/leagues` gives an id and a name for every league OpenDota knows.

    Only the name is taken. `tier` on this endpoint is Valve's own label, which is not the
    Tier 1 classification the product means (spec section 3) - that one is decided against
    Liquipedia by `map-leagues`, and overwriting it here would undo hand-checked work.
    """
    return [
        {"league_id": int(row["leagueid"]), "name": name}
        for row in payload
        if row.get("leagueid") and (name := row.get("name"))
    ]


async def refresh_league_names(client: LeagueSource, session_factory: Any) -> int:
    """Fill in league names from the one call that carries all of them.

    Nothing was calling `/leagues` at all, so a league first seen by the live poller had an
    id and no name for good: Valve's scoreboard does not carry one, and `/proMatches` only
    covers leagues whose matches reach that endpoint.

    Names only, and only where the row already exists or arrives new - the tier, prize pool
    and Liquipedia slug are owned by `map-leagues` and left untouched.
    """
    rows = parse_leagues(await client.leagues())
    async with session_factory() as session:
        written = await _upsert(session, League, rows, "league_id")
        await session.commit()
    log.info("reference.leagues", written=written)
    return written
