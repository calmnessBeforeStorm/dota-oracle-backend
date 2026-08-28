"""Match detail backfill (spec sections 2.2/A2, 2.3, phase 1).

One call per map, from either source, and the largest consumer of quota either way. Which
source is in play changes what "too fast" means:

  - **OpenDota** (`GET /matches/{id}`) is bound by response time rather than by its stated
    60 req/min - 150 maps took 9m04s, about 16.5 a minute - and the constraint that actually
    stops a run is the *daily* allowance: without an API key one was cut off after 685
    matches with a 429 carrying no Retry-After. Plan in days.
  - **STRATZ** (`match(id:)`) allows ~2000 requests an hour, so the same 685 maps take
    about twenty minutes. This is the default, and the reason the phase-3 dataset stopped
    being quota-bound.

This is the payload the whole project is built on: rosters, draft, the building log and,
for parsed matches, the per-minute series that phase 3 turns into snapshots. The two
sources do not report the same quantity in those series - see
docs/superpowers/specs/2026-08-27-stratz-adapter-design.md - which is why the source a row
was fetched from is part of its identity in `raw_matches` and never merged away.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.db.models.matches import Match
from app.db.models.raw import RawMatch
from app.db.session import get_session_factory
from app.ingestion.clients.base import RateLimitedError
from app.ingestion.clients.opendota import OpenDotaClient
from app.ingestion.clients.stratz import StratzClient
from app.ingestion.repository import upsert_raw_matches
from app.ingestion.sources import RawSource

log = get_logger(__name__)

#: Give up after this many failures in a row - something is wrong upstream, and hammering it
#: is neither polite nor productive.
#:
#: "In a row" is the correction, not the number. The check used to also require that nothing
#: had been fetched yet, which meant a run that started fine and hit a wall later never
#: stopped: the STRATZ backfill worked through 1600 remaining ids at one rejected request
#: every two seconds, logging a failure each time and reporting exit code 0 at the end.
CONSECUTIVE_FAILURE_LIMIT = 20


class MatchDetailSource(Protocol):
    async def match(self, match_id: int) -> dict[str, Any]: ...


@dataclass
class DetailsReport:
    requested: int = 0
    fetched: int = 0
    failed: int = 0
    remaining: int = 0
    stopped_because: str = "finished the list"

    def as_log_fields(self) -> dict[str, Any]:
        return {
            "requested": self.requested,
            "fetched": self.fetched,
            "failed": self.failed,
            "remaining": self.remaining,
            "stopped_because": self.stopped_because,
        }


async def select_matches_missing_details(
    session: AsyncSession,
    limit: int,
    newest_first: bool = True,
    source: RawSource = RawSource.OPENDOTA_MATCH,
) -> list[int]:
    """Maps we know about but have no detail payload for, from this source.

    Per source, and not merged: a map fetched from OpenDota is still missing from STRATZ,
    so one shared counter would call the backfill finished with half of it unrun.

    Newest first by default: recent patches are the ones the model is asked about, and a
    backfill that is stopped halfway should have covered the most useful history.
    """
    already = select(RawMatch.match_id).where(RawMatch.source == str(source))
    statement = (
        select(Match.match_id)
        .where(Match.match_id.notin_(already))
        .order_by(Match.start_time.desc() if newest_first else Match.start_time.asc())
        .limit(limit)
    )
    return list((await session.execute(statement)).scalars().all())


async def count_missing_details(
    session: AsyncSession, source: RawSource = RawSource.OPENDOTA_MATCH
) -> int:
    already = select(RawMatch.match_id).where(RawMatch.source == str(source))
    statement = select(Match.match_id).where(Match.match_id.notin_(already))
    return len((await session.execute(statement)).scalars().all())


async def fetch_details(
    client: MatchDetailSource,
    session_factory: async_sessionmaker[AsyncSession],
    match_ids: Sequence[int],
    source: RawSource,
) -> DetailsReport:
    """Fetch and store one payload per id. Safe to stop at any point.

    Split out from the backfill because the same loop serves a second question: not "which
    maps are missing a payload" but "which matches do we owe an outcome for" (see
    `app.ingestion.workers.outcomes`). Only the list of ids differs, and the parts that must
    not diverge - rate-limit handling, per-match commits - are exactly the parts here.
    """
    report = DetailsReport(requested=len(match_ids))
    consecutive_failures = 0

    log.info("details.start", requested=report.requested, source=str(source))

    for match_id in match_ids:
        try:
            payload = await client.match(match_id)
        except RateLimitedError as exc:
            # The quota is gone. Working through the rest of the list at one failure per
            # second fetches nothing and earns an IP ban; what is already stored is the
            # checkpoint, so resuming later costs nothing.
            report.stopped_because = f"rate limited after {report.fetched} matches"
            log.warning("details.rate_limited", fetched=report.fetched, error=str(exc))
            break
        except Exception as exc:
            report.failed += 1
            consecutive_failures += 1
            log.warning("details.failed", match_id=match_id, error=str(exc))
            if consecutive_failures >= CONSECUTIVE_FAILURE_LIMIT:
                report.stopped_because = (
                    f"upstream failed {consecutive_failures} times in a row "
                    f"after {report.fetched} matches"
                )
                log.warning("details.giving_up", **report.as_log_fields())
                break
            continue

        # Commit per match: the run takes hours and will be interrupted, and a call already
        # paid for must never have to be paid for twice.
        async with session_factory() as session:
            await upsert_raw_matches(session, source, [payload])
            await session.commit()
        report.fetched += 1
        consecutive_failures = 0

    log.info("details.done", source=str(source), **report.as_log_fields())
    return report


async def run_details_backfill(
    client: MatchDetailSource,
    session_factory: async_sessionmaker[AsyncSession],
    limit: int = 100,
    newest_first: bool = True,
    source: RawSource = RawSource.OPENDOTA_MATCH,
) -> DetailsReport:
    """Fetch detail payloads for maps that lack one."""
    async with session_factory() as session:
        match_ids = await select_matches_missing_details(session, limit, newest_first, source)

    report = await fetch_details(client, session_factory, match_ids, source)

    async with session_factory() as session:
        report.remaining = await count_missing_details(session, source)
    return report


#: The two ways to fetch a map's per-minute series. STRATZ is the default because it is the
#: one the training set is built from; OpenDota stays reachable for comparison and for the
#: objectives log.
DETAIL_SOURCES: dict[str, tuple[RawSource, type[OpenDotaClient] | type[StratzClient]]] = {
    "stratz": (RawSource.STRATZ_MATCH, StratzClient),
    "opendota": (RawSource.OPENDOTA_MATCH, OpenDotaClient),
}


async def backfill_match_details(
    ctx: dict[str, Any],
    limit: int = 100,
    newest_first: bool = True,
    source: str = "stratz",
) -> int:
    """arq entry point. Returns payloads fetched."""
    raw_source, client_type = DETAIL_SOURCES[source]
    async with client_type() as client:
        report = await run_details_backfill(
            client,
            get_session_factory(),
            limit=limit,
            newest_first=newest_first,
            source=raw_source,
        )
    return report.fetched


#: What the hourly cron asks for, and deliberately short of the STRATZ allowance.
#:
#: The same quota covers `resolve_prediction_outcomes`, and that one is not optional: a
#: prediction nobody can score teaches nothing, and it needs a handful of calls against this
#: job's hundreds. Measured 2026-08-28, an unbounded run was refused after 476 maps - the
#: whole hour's room, spent by whichever job happened to start first.
DETAILS_PER_HOUR = 300


async def backfill_details_hourly(ctx: dict[str, Any]) -> int:
    """arq cron entry point: one bounded slice of the history backfill.

    A separate coroutine because `arq.cron` takes no job arguments, and because the bound is
    a scheduling decision rather than a default of the worker - a run started by hand should
    still be free to ask for as much as it likes.
    """
    return await backfill_match_details(ctx, limit=DETAILS_PER_HOUR, source="stratz")
