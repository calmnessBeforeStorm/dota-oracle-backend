"""arq worker entrypoint (spec sections 9.1, 10).

Run with:  arq app.workers.settings.WorkerSettings
"""

from typing import Any, ClassVar

from arq.connections import RedisSettings
from arq.cron import cron

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.ingestion.workers.backfill import backfill_pro_matches, catch_up_pro_matches
from app.ingestion.workers.details import backfill_match_details
from app.ingestion.workers.live_poller import poll_live_games
from app.ingestion.workers.outcomes import resolve_prediction_outcomes
from app.ingestion.workers.sync import sync_liquipedia


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    ctx["settings"] = settings


async def shutdown(ctx: dict[str, Any]) -> None:
    from app.core.redis import close_redis
    from app.db.session import dispose_engine

    await close_redis()
    await dispose_engine()


class WorkerSettings:
    redis_settings = RedisSettings(
        host=get_settings().redis_host,
        port=get_settings().redis_port,
        database=get_settings().redis_db,
    )
    functions: ClassVar[list[Any]] = [
        backfill_pro_matches,
        backfill_match_details,
        poll_live_games,
        sync_liquipedia,
        resolve_prediction_outcomes,
    ]
    cron_jobs: ClassVar[list[Any]] = [
        # Live loop: every 30s, per spec section 2.4.
        cron(poll_live_games, second={0, 30}, run_at_startup=True, max_tries=1),
        # Liquipedia: no more than hourly, everything else served from cache.
        cron(sync_liquipedia, minute=7, max_tries=2),
        # Pick up matches that finished since the last pass. Hourly and cheap: one call when
        # nothing is new. Unlike the historical backfill this one is safe to schedule - it
        # cannot run away, because it stops the moment it reaches what is already stored.
        # Without it the dataset ends on the day the backfill started and no prediction the
        # live loop makes can ever be scored against an outcome.
        cron(catch_up_pro_matches, minute=23, max_tries=2),
        # Ask STRATZ directly about the matches we have already predicted. The summary
        # feed reaches them eventually; "eventually" is not a property the accuracy
        # dashboard or the drift alert can be built on, and a served prediction nobody
        # ever scores is a prediction that taught us nothing.
        cron(resolve_prediction_outcomes, minute=41, max_tries=2),
    ]
    on_startup = startup
    on_shutdown = shutdown
    max_jobs = 10
