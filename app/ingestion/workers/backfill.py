"""Historical backfill from OpenDota (spec sections 2.2, 4.4, phase 1).

Two walks over `/proMatches`, ~100 matches per call, storing every payload whole.
`run_backfill` goes backwards to deepen history; `catch_up` goes forward to pick up matches
played since. They are separate because one cursor cannot describe a window that grows at
both ends, and without the forward one the dataset silently stops at the day the backfill
started. Nothing is parsed here on purpose: the raw layer is the thing we can never re-fetch
cheaply (quotas, hours of wall clock, sources that disappear), while the normalized layer
can always be rebuilt from it.

Resumability is the design constraint. One year of pro matches is roughly 25 000 calls, about
seven hours at 60 req/min, so the run *will* be interrupted. Progress therefore lives in
`ingest_checkpoints` and every page commits on its own.
"""

from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.db.session import get_session_factory
from app.ingestion.clients.opendota import OpenDotaClient
from app.ingestion.repository import get_checkpoint, set_checkpoint, upsert_raw_matches
from app.ingestion.sources import Checkpoint, RawSource

log = get_logger(__name__)


class ProMatchesSource(Protocol):
    """Just the slice of OpenDotaClient the backfill needs, so tests can pass a fake."""

    async def pro_matches(self, less_than_match_id: int | None = None) -> list[dict[str, Any]]: ...


@dataclass
class BackfillReport:
    pages: int = 0
    rows: int = 0
    lowest_match_id: int | None = None
    stopped_because: str = "page limit reached"

    def as_log_fields(self) -> dict[str, Any]:
        return {
            "pages": self.pages,
            "rows": self.rows,
            "lowest_match_id": self.lowest_match_id,
            "stopped_because": self.stopped_because,
        }


async def run_backfill(
    client: ProMatchesSource,
    session_factory: async_sessionmaker[AsyncSession],
    pages: int = 1,
    restart: bool = False,
) -> BackfillReport:
    """Fetch `pages` pages of pro matches, oldest-ward from the stored cursor.

    `restart=True` ignores the checkpoint and starts again from the newest matches. Existing
    rows are refreshed rather than duplicated, so a restart is safe, just wasteful.
    """
    report = BackfillReport()

    async with session_factory() as session:
        cursor_value = (
            None if restart else await get_checkpoint(session, Checkpoint.OPENDOTA_PRO_MATCHES)
        )
        cursor = int(cursor_value) if cursor_value else None

    log.info("backfill.start", cursor=cursor, pages=pages, restart=restart)

    for page in range(pages):
        batch = await client.pro_matches(less_than_match_id=cursor)
        if not batch:
            report.stopped_because = "upstream returned an empty page"
            break

        match_ids = [int(m["match_id"]) for m in batch if m.get("match_id") is not None]
        if not match_ids:
            report.stopped_because = "page carried no usable match_id"
            break

        cursor = min(match_ids)

        # One transaction per page: an interrupt costs at most the page in flight.
        async with session_factory() as session:
            written = await upsert_raw_matches(session, RawSource.OPENDOTA_PRO_MATCHES, batch)
            await set_checkpoint(session, Checkpoint.OPENDOTA_PRO_MATCHES, str(cursor))
            await session.commit()

        report.pages = page + 1
        report.rows += written
        report.lowest_match_id = cursor
        log.info("backfill.page", page=page + 1, rows=written, cursor=cursor)

    log.info("backfill.done", **report.as_log_fields())
    return report


async def catch_up(
    client: ProMatchesSource,
    session_factory: async_sessionmaker[AsyncSession],
    max_pages: int = 20,
) -> BackfillReport:
    """Fetch matches played since the last run, newest first.

    `run_backfill` cannot do this. It pages with `less_than_match_id` from a cursor that only
    ever moves further into the past, so a match played today is invisible to it - and
    measured on real data, all 212 matches the live poller had predicted sat above the newest
    match in the table. Nothing could ever be scored against its outcome, which quietly makes
    the accuracy dashboard (F6) and any drift alert impossible rather than merely empty.

    The two walks are opposite ends of one growing range, so they keep separate cursors: this
    one records the highest match id ever stored and stops as soon as a page reaches it.
    Nothing new means exactly one call.
    """
    report = BackfillReport(stopped_because="page limit reached")

    async with session_factory() as session:
        seen_value = await get_checkpoint(session, Checkpoint.OPENDOTA_PRO_MATCHES_NEWEST)
    newest_seen = int(seen_value) if seen_value else None
    highest = newest_seen

    log.info("catch_up.start", newest_seen=newest_seen, max_pages=max_pages)

    cursor: int | None = None
    for page in range(max_pages):
        batch = await client.pro_matches(less_than_match_id=cursor)
        if not batch:
            report.stopped_because = "upstream returned an empty page"
            break

        match_ids = [int(m["match_id"]) for m in batch if m.get("match_id") is not None]
        if not match_ids:
            report.stopped_because = "page carried no usable match_id"
            break

        # Store the whole page even when it straddles the boundary: re-storing a row we
        # already hold refreshes it, and dropping one to avoid that risks a gap.
        async with session_factory() as session:
            written = await upsert_raw_matches(session, RawSource.OPENDOTA_PRO_MATCHES, batch)
            await session.commit()

        report.pages = page + 1
        report.rows += written
        highest = max(highest or 0, max(match_ids))
        cursor = min(match_ids)
        log.info("catch_up.page", page=page + 1, rows=written, cursor=cursor)

        if newest_seen is not None and cursor <= newest_seen:
            report.stopped_because = "reached matches already stored"
            break

    if highest is not None and highest != newest_seen:
        async with session_factory() as session:
            await set_checkpoint(session, Checkpoint.OPENDOTA_PRO_MATCHES_NEWEST, str(highest))
            await session.commit()

    report.lowest_match_id = cursor
    log.info("catch_up.done", newest=highest, **report.as_log_fields())
    return report


async def backfill_pro_matches(ctx: dict[str, Any], pages: int = 1, restart: bool = False) -> int:
    """arq entry point. Returns rows written."""
    async with OpenDotaClient() as client:
        report = await run_backfill(client, get_session_factory(), pages=pages, restart=restart)
    return report.rows


async def catch_up_pro_matches(ctx: dict[str, Any], max_pages: int = 20) -> int:
    """arq entry point for the forward walk. Cheap enough to run on a schedule."""
    async with OpenDotaClient() as client:
        report = await catch_up(client, get_session_factory(), max_pages=max_pages)
    return report.rows
