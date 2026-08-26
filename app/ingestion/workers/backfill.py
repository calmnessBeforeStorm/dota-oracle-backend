"""Historical backfill (spec sections 2.2/A4, 4.4, phase 1).

Walks /proMatches backwards, then pulls match details. Idempotent: everything is upserted
by natural key, and progress lives in `ingest_checkpoints`, so a restarted worker resumes
instead of duplicating. 429 is honoured via Retry-After, not by blind retry.
"""

from typing import Any

from app.core.logging import get_logger

log = get_logger(__name__)


async def backfill_pro_matches(ctx: dict[str, Any], until_match_id: int | None = None) -> int:
    """Fetch one page of pro matches and persist raw payloads. Returns rows written.

    TODO(phase-1): implement. Budget check first - one year of pro matches is ~25k
    OpenDota calls (~7 hours at 60 req/min); beyond that, use STRATZ for the skeleton.
    """
    log.info("backfill.tick", implemented=False, until_match_id=until_match_id)
    return 0
