"""arq worker entrypoint (spec sections 9.1, 10).

Run with:  arq app.workers.settings.WorkerSettings
"""

from typing import Any, ClassVar

from arq.connections import RedisSettings
from arq.cron import cron

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.ingestion.workers.backfill import backfill_pro_matches, catch_up_pro_matches
from app.ingestion.workers.details import backfill_details_hourly, backfill_match_details
from app.ingestion.workers.live_poller import poll_live_games
from app.ingestion.workers.normalize_worker import normalize_stored_payloads
from app.ingestion.workers.outcomes import resolve_prediction_outcomes
from app.ingestion.workers.sync import sync_liquipedia
from app.workers.drift import check_calibration_drift


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
        backfill_details_hourly,
        poll_live_games,
        sync_liquipedia,
        resolve_prediction_outcomes,
        normalize_stored_payloads,
        check_calibration_drift,
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
        # The history backfill, which had no schedule at all and only advanced when somebody
        # ran the CLI. 23688 maps remain, so at a few hundred an hour it is days of work that
        # nobody should have to babysit.
        #
        # Bounded per run rather than left to fetch until it is refused - see
        # `DETAILS_PER_HOUR`. Never retried: a failed slice is not worth a second helping of
        # somebody else's quota, and the next hour picks up exactly where it stopped.
        cron(backfill_details_hourly, minute=11, max_tries=1),
        # Six minutes behind the outcome resolver, because it is what finishes that job:
        # the resolver stores a payload and this is the step that reads the outcome out of
        # it. Without it the chain ended in the raw table - every job green, the accuracy
        # dashboard empty, and nothing anywhere saying why.
        #
        # The gap is a guess, not a measurement: the resolver's own run time is unbounded
        # (it fetches one match at a time and its queue is however many predictions went
        # unscored). Missing the gap costs an hour of latency, not a row - both jobs are
        # idempotent and the next pass picks up whatever the last one was too early for.
        cron(normalize_stored_payloads, minute=47, max_tries=1),
        # Phase 7. Once a day rather than hourly: the window is seven days wide, so an
        # hourly verdict would be the same verdict twenty-four times, and an alert that
        # repeats itself all day is one people learn to close.
        cron(check_calibration_drift, hour=6, minute=17, max_tries=2),
    ]
    on_startup = startup
    on_shutdown = shutdown
    max_jobs = 10
