"""Keeping the training set level with the outcomes, on a schedule (phases 3, 11).

Everything upstream of this already runs by itself: the poller predicts, `catch_up` and
`resolve_prediction_outcomes` find the matches that finished, `backfill_details_hourly` pulls
the payload each one needs, and `normalize_stored_payloads` turns all of it into rows. Then
it stopped. `prematch` and `featurize` only ever advanced when a person typed them, so the
dataset the model is fitted on lagged the outcomes by however long it had been since somebody
last ran the pipeline by hand - and nothing said so, because a stale table looks exactly like
a fresh one.

**The two run together or not at all.** `featurize` reads each map's pre-match row for the
prior and the eight pre-match differences, and where there is no row it falls back to a prior
of 0.5 and an empty feature block. Scheduling `featurize` alone would therefore quietly write
defaults into the same columns that hold measured values everywhere else - the same defect
`is_lan` had, where the model learns our coverage instead of the game, and no metric shows
it. So `prematch` runs first, every time.

**No truncate on a cron, and it turns out not to need one.** `featurize --rebuild` empties
the table before writing, which is not something to hand to a scheduler. It is also not what
this job needs: `featurize` has no cursor, so it walks every stored payload and upserts every
row on each pass, which means a plain run already rewrites the whole table with the current
feature set and the freshly rebuilt priors. What `--rebuild` adds is the removal of rows the
current pass would no longer produce - a shrunk feature set, a changed source, a filter that
now excludes a map. Those are things a person does deliberately, and they stay a manual step.
"""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.db.session import get_session_factory
from app.features.featurize import featurize
from app.features.prematch import rebuild_prematch

log = get_logger(__name__)


async def run_training_set_refresh(session_factory: async_sessionmaker[AsyncSession]) -> int:
    """Rebuild pre-match features, then the snapshots that read them. Returns snapshots written.

    Order is the invariant, not a preference: `rebuild_prematch` walks the matches in
    chronological order and records the state *before* each one, and `featurize` reads what it
    wrote. Reversed, every snapshot carries a prior of 0.5.

    The session factory is a parameter rather than fetched inside, so this is testable against
    a throwaway schema instead of only against production.
    """
    prematch = await rebuild_prematch(session_factory)
    snapshots = await featurize(session_factory)

    log.info(
        "training_set.refreshed",
        prematch_rows=prematch.rows_written,
        matches_used=snapshots.matches_used,
        snapshots=snapshots.snapshots,
        skipped=snapshots.skipped,
    )
    return snapshots.snapshots


async def refresh_training_set(ctx: dict[str, Any]) -> int:
    """arq entry point. Returns snapshots written."""
    return await run_training_set_refresh(get_session_factory())
