"""Liquipedia sync (spec sections 2.5, 3, 5.5, phase 2).

Hourly at most. Fills `leagues` (tier marking) and `tournament_stages` - the source of
truth for series format, including Bo2, which Valve data cannot express.
"""

from typing import Any

from app.core.logging import get_logger

log = get_logger(__name__)


async def sync_liquipedia(ctx: dict[str, Any]) -> int:
    """Refresh tournaments, stages and schedule. Returns rows touched.

    TODO(phase-2): implement. Order matters: tournaments -> stages (default_format,
    points_rule) -> series -> match linkage. Cache everything; respect the rate limit.
    """
    log.info("liquipedia_sync.tick", implemented=False)
    return 0
