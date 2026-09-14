"""One-off fill of `predictions.league_id` and `predictions.valve_tier` (design 2026-09-11).

Both columns appeared after the live loop had been logging for days. The league is exact: the
poller's own snapshot of the match names it. The Valve tier is not point-in-time - it is the
league's tier *now* - which is acceptable for rows a few days old and would not be for an
archive: Valve re-tags leagues over months (34% of the summary archive is in leagues that are
`excluded` today). Run after `reference`, which is what loads the tiers.

Only NULLs are filled. A value already present was written at serve time and is better
knowledge than anything this pass has.
"""

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

LEAGUE_FROM_SNAPSHOTS = text(
    """
    UPDATE predictions AS p
    SET league_id = s.league_id
    FROM (
        SELECT DISTINCT ON (match_id)
            match_id,
            NULLIF(payload->>'league_id', '0')::bigint AS league_id
        FROM raw_live_snapshots
        WHERE payload->>'league_id' IS NOT NULL
        ORDER BY match_id, captured_at DESC
    ) AS s
    WHERE p.match_id = s.match_id
      AND p.league_id IS NULL
      AND s.league_id IS NOT NULL
    """
)

TIER_FROM_LEAGUES = text(
    """
    UPDATE predictions AS p
    SET valve_tier = l.valve_tier
    FROM leagues AS l
    WHERE p.league_id = l.league_id
      AND p.valve_tier IS NULL
      AND l.valve_tier IS NOT NULL
    """
)


@dataclass(frozen=True)
class SegmentBackfillReport:
    league_ids: int
    valve_tiers: int


async def backfill_segments(
    session_factory: async_sessionmaker[AsyncSession],
) -> SegmentBackfillReport:
    async with session_factory() as session:
        leagues = await session.execute(LEAGUE_FROM_SNAPSHOTS)
        tiers = await session.execute(TIER_FROM_LEAGUES)
        await session.commit()
    return SegmentBackfillReport(
        # CursorResult carries rowcount; the base Result type mypy infers here does not.
        league_ids=int(leagues.rowcount or 0),  # type: ignore[attr-defined]
        valve_tiers=int(tiers.rowcount or 0),  # type: ignore[attr-defined]
    )
