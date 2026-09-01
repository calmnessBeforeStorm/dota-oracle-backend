"""The cron schedule has to survive its own jobs finishing.

arq gives every job a 300-second default timeout, which is shorter than two of the jobs
scheduled here actually take. Nothing about that looked like a bug from the outside: the
worker logged `TimeoutError` and moved on, the backfill kept advancing at a plausible pace,
and the half of the budget it never spent read as an upstream problem.

These tests hold the arithmetic that connects a job's budget to its slot, because the two
drifted apart silently once already.
"""

from arq.cron import CronJob

from app.ingestion.clients.stratz import StratzClient
from app.ingestion.workers.details import (
    DETAILS_PER_HOUR,
    backfill_details_hourly,
    stratz_slice_timeout,
)
from app.ingestion.workers.normalize_worker import normalize_stored_payloads
from app.ingestion.workers.outcomes import OUTCOMES_PER_RUN, resolve_prediction_outcomes
from app.workers.settings import WorkerSettings

#: What arq falls back to, and what both STRATZ jobs were being killed by.
ARQ_DEFAULT_JOB_TIMEOUT = 300


def _job(coroutine: object) -> CronJob:
    name = coroutine.__name__  # type: ignore[attr-defined]
    matches = [job for job in WorkerSettings.cron_jobs if job.name == f"cron:{name}"]
    assert len(matches) == 1, f"{name} is not scheduled exactly once"
    return matches[0]


def test_slice_timeout_is_the_throttle_and_not_a_guess() -> None:
    """300 maps at one every two seconds is 600 seconds of waiting, plus head-room."""
    assert stratz_slice_timeout(300) == 300 * StratzClient.min_interval * 1.5
    assert stratz_slice_timeout(DETAILS_PER_HOUR) >= DETAILS_PER_HOUR * StratzClient.min_interval


def test_details_backfill_is_allowed_to_finish_its_budget() -> None:
    """The regression: it asked for 300 maps and was killed after 150, every hour."""
    job = _job(backfill_details_hourly)
    needed = DETAILS_PER_HOUR * StratzClient.min_interval

    assert needed > ARQ_DEFAULT_JOB_TIMEOUT, "budget no longer needs an explicit timeout"
    assert job.timeout_s is not None, "would fall back to arq's 300s and be killed halfway"
    assert job.timeout_s >= needed


def test_outcome_resolver_is_allowed_to_finish_a_full_queue() -> None:
    """Same cliff, and it only bites after an outage - when the queue is finally full."""
    job = _job(resolve_prediction_outcomes)
    needed = OUTCOMES_PER_RUN * StratzClient.min_interval

    assert needed > ARQ_DEFAULT_JOB_TIMEOUT
    assert job.timeout_s is not None
    assert job.timeout_s >= needed


def test_normalization_starts_after_the_resolver_can_still_be_running() -> None:
    """Normalization reads the outcome out of what the resolver stored, so it has to run
    behind the resolver's *worst* case rather than behind its typical one."""
    resolver = _job(resolve_prediction_outcomes)
    normalize = _job(normalize_stored_payloads)

    worst_case_seconds = OUTCOMES_PER_RUN * StratzClient.min_interval
    assert isinstance(resolver.minute, int) and isinstance(normalize.minute, int)

    gap_seconds = (normalize.minute - resolver.minute) * 60
    assert gap_seconds > worst_case_seconds, (
        f"normalize at :{normalize.minute} can start while the resolver is still fetching"
    )


def test_every_stratz_job_declares_a_timeout() -> None:
    """Anything that spends the STRATZ throttle outlives arq's default. New ones too."""
    for coroutine in (backfill_details_hourly, resolve_prediction_outcomes):
        assert _job(coroutine).timeout_s is not None


def test_liveness_record_expires_fast_enough_to_mean_something() -> None:
    """arq's key lives `health_check_interval + 1` seconds and the container polls every 30.

    The default is an hour, which would let a dead worker keep passing its healthcheck for
    an hour. The previous check was the API image's `curl localhost:8000` inherited by a
    process that serves no HTTP - it failed always, which is the same amount of information.
    """
    assert WorkerSettings.health_check_interval <= 60


def test_the_training_set_has_a_schedule_at_all() -> None:
    """The last link that only moved when a person typed it (phases 3, 11)."""
    from app.workers.training_set import refresh_training_set

    job = _job(refresh_training_set)
    assert job.timeout_s is not None and job.timeout_s > ARQ_DEFAULT_JOB_TIMEOUT
