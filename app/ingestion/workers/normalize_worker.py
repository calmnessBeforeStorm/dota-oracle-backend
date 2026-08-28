"""Turning stored payloads into the normalized layer, on a schedule.

Every other link in the chain runs by itself. The poller predicts, `catch_up` finds matches
that have finished, `resolve_prediction_outcomes` fetches the payload behind a prediction
nobody can score yet - and then nothing happened, because the step that reads an outcome out
of that payload only ever ran when a person typed it.

The effect was invisible in the way that costs the most: every job reported success, the raw
table filled up, and the accuracy dashboard stayed empty for a model that had been serving
all day. `outcomes.py` even says in a comment that the gap "belongs to `normalize`". It
belonged to nobody.

Safe to schedule because normalization is idempotent by construction (invariant 5): it reads
`raw_matches`, writes upserts keyed on natural keys, and touches no network. Running it twice
produces what running it once produced.

What this job does NOT do, so nobody reads more into it than it delivers:

- It closes the gap up to `matches`, and no further. `prematch` and `featurize --rebuild`
  still only advance when a person types them, which means the training set keeps lagging
  the outcomes by however long it has been since somebody last ran the pipeline by hand.
  Scheduling those two is the next step and deliberately not taken here: `featurize
  --rebuild` truncates before it writes, and a truncate on a cron wants more thought than
  a job that only upserts.
- It has never been observed firing on the real cron. The behaviour is covered by tests and
  the pass was run by hand against the live database, but the scheduled path itself - arq
  picking it up at :47, on a worker that has been restarted - is unproven.
- The full-pass cost is unmeasured on today's archive. "About two minutes" below is an
  estimate from a manual run, not a timing, and it grows with every backfilled match.
"""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.db.session import get_session_factory
from app.ingestion.normalize import normalize_match_details, normalize_pro_matches

log = get_logger(__name__)


async def run_normalize(session_factory: async_sessionmaker[AsyncSession]) -> int:
    """One full pass over the stored payloads. Returns matches written from the summaries.

    A full pass rather than an incremental one. It re-reads every payload, which costs about
    two minutes today and will grow with the archive - the honest trade for a step that has
    no cursor and cannot be resumed halfway. When that becomes the slowest thing in the hour,
    the fix is a cursor on `raw_matches`, not a shorter interval.

    The session factory is a parameter rather than fetched inside, which is the difference
    between a testable job and one that can only be run against production. Written the other
    way first, and the test caught it by normalising the real database instead of its own
    throwaway schema.
    """
    summaries = await normalize_pro_matches(session_factory)
    details = await normalize_match_details(session_factory)

    log.info(
        "normalize.done",
        matches=summaries.matches,
        series=summaries.series,
        detail_payloads=details.raw_seen,
        players=details.match_players,
    )
    return summaries.matches


async def normalize_stored_payloads(ctx: dict[str, Any]) -> int:
    """arq entry point. Returns matches written from the summary layer."""
    return await run_normalize(get_session_factory())
