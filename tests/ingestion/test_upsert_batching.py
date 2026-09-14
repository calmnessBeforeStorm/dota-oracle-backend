"""Reference upserts must survive a table bigger than one Bind message.

Postgres binds parameters with an int16 count, so a single statement carries at most 32767 of
them. The reference loaders send one row per league, hero or pro player in a single
`INSERT ... ON CONFLICT`, so the ceiling is a row count, not a byte count - and it was crossed
the day `valve_tier` became a fourth column on `/leagues`: 10205 rows x 4 parameters = 40820,
and `reference` died on production with

    asyncpg.exceptions._base.InterfaceError: the number of query arguments cannot exceed 32767

The failure is silent until it happens and then total: no league gets a name, no league gets a
Valve tier, and the live feed stays empty because every tier is unknown. `/proPlayers` was
twenty-one parameters short of the same wall at 5456 rows of six, so this is not a `/leagues`
quirk to patch at the call site.
"""

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.reference import League
from app.ingestion.reference import MAX_BIND_PARAMS, _upsert

#: Enough rows that one statement would need more parameters than the protocol allows. Counted
#: against the three columns these fixture rows name: 12000 x 3 = 36000, past the 32767 ceiling.
#: Production crossed it with fewer rows because SQLAlchemy binds a parameter for `leagues.tier`
#: too - a column default the parser never names - making it four per row at 10205 rows.
ROWS_OVER_THE_LIMIT = 12000


def league_rows(count: int, *, valve_tier: str = "professional") -> list[dict[str, Any]]:
    return [
        {"league_id": 900000 + i, "name": f"League {i}", "valve_tier": valve_tier}
        for i in range(count)
    ]


async def test_writes_more_rows_than_one_bind_message_can_carry(session: AsyncSession) -> None:
    rows = league_rows(ROWS_OVER_THE_LIMIT)
    assert len(rows) * len(rows[0]) > 32767, "fixture no longer exceeds the protocol limit"

    written = await _upsert(session, League, rows, "league_id")

    assert written == ROWS_OVER_THE_LIMIT
    stored = await session.scalar(select(func.count()).select_from(League))
    assert stored == ROWS_OVER_THE_LIMIT


async def test_a_second_pass_updates_rather_than_duplicates(session: AsyncSession) -> None:
    """The chunking must not cost idempotence (invariant 5): the conflict clause still owns
    every chunk, so a re-run overwrites the same rows instead of failing or doubling them."""
    await _upsert(session, League, league_rows(ROWS_OVER_THE_LIMIT), "league_id")

    await _upsert(
        session, League, league_rows(ROWS_OVER_THE_LIMIT, valve_tier="excluded"), "league_id"
    )

    stored = await session.scalar(select(func.count()).select_from(League))
    assert stored == ROWS_OVER_THE_LIMIT
    tier = await session.scalar(select(League.valve_tier).where(League.league_id == 900000))
    assert tier == "excluded"


async def test_a_small_batch_is_still_one_statement(session: AsyncSession) -> None:
    """The chunking is a ceiling, not a rewrite of how small loads are written."""
    written = await _upsert(session, League, league_rows(3), "league_id")

    assert written == 3
    assert await session.scalar(select(func.count()).select_from(League)) == 3


def test_the_budget_leaves_room_for_columns_the_rows_do_not_name() -> None:
    """`rows` carries the columns the parser filled; SQLAlchemy adds a bound parameter for every
    column default it renders too - `leagues.tier` among them. The budget is well under the
    protocol's 32767 so those extras cannot push a chunk over."""
    assert MAX_BIND_PARAMS <= 32767 // 1.5
