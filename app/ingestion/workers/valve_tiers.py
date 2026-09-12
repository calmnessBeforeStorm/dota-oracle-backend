"""Valve's league tiers, refreshed hourly and on request (design 2026-09-11-pro-segment).

The live feed shows only professional leagues, and a league's Valve tier is known only once
`/leagues` has been asked since the league appeared. Hourly alone would hide a new Tier 1
tournament for up to an hour - a whole map - so the poller may also request a refresh.

It may want one every tick, for a league OpenDota simply does not list. The throttle is
therefore inside the job, keyed in Redis, and holds whoever asked: at most one call per
`REFRESH_EVERY_SECONDS`. The request checks the same key before queueing, so a closed window
costs one EXISTS per tick rather than a job run and a log line. `/leagues` is a megabyte,
which is also why the poller enqueues this rather than calling it inside its thirty-second
tick.
"""

from typing import Any

from app.core.logging import get_logger
from app.core.redis import get_redis
from app.db.session import get_session_factory
from app.ingestion.clients.base import RateLimitedError
from app.ingestion.clients.opendota import OpenDotaClient
from app.ingestion.reference import LeagueSource, refresh_league_names

log = get_logger(__name__)

REFRESH_VALVE_TIERS_JOB = "refresh_valve_tiers"
THROTTLE_KEY = "valve_tiers:throttle"
REFRESH_EVERY_SECONDS = 15 * 60


async def refresh_valve_tiers_once(
    client: LeagueSource, session_factory: Any, redis: Any
) -> int | None:
    """Leagues written, 0 on failure, or None when the window is still closed.

    The throttle key is claimed before the call and left in place on failure: a refusal is
    exactly when asking again sooner does harm.
    """
    if not await redis.set(THROTTLE_KEY, "1", nx=True, ex=REFRESH_EVERY_SECONDS):
        log.info("valve_tiers.skipped")
        return None
    try:
        return await refresh_league_names(client, session_factory)
    except RateLimitedError as exc:
        log.warning("valve_tiers.rate_limited", retry_after=exc.retry_after)
    except Exception as exc:
        log.warning("valve_tiers.failed", error=str(exc))
    return 0


async def refresh_valve_tiers(ctx: dict[str, Any]) -> int:
    """arq entry point, cron and on request alike."""
    async with OpenDotaClient() as client:
        written = await refresh_valve_tiers_once(client, get_session_factory(), get_redis())
    return written or 0


async def request_valve_tier_refresh(ctx: dict[str, Any]) -> bool:
    """Ask the worker for a refresh. True when a job was queued.

    Nothing is queued while the throttle window is closed: a league OpenDota does not list
    would otherwise queue a job every tick, and the worker would run it 120 times an hour only
    to log "skipped". The fixed job id makes arq drop duplicates while one is waiting. arq's
    pool and `get_redis()` point at the same database (host, port and db from one settings
    object), so the key the job sets is the key checked here.
    """
    redis = ctx.get("redis")
    if redis is None:
        return False
    try:
        if await redis.exists(THROTTLE_KEY):
            return False
        job = await redis.enqueue_job(REFRESH_VALVE_TIERS_JOB, _job_id=REFRESH_VALVE_TIERS_JOB)
    except Exception as exc:
        log.warning("valve_tiers.enqueue_failed", error=str(exc))
        return False
    return job is not None
