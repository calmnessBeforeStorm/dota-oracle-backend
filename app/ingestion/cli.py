"""Manual entry point for ingestion runs.

The backfill is a long, interruptible job that a human starts and watches, not something the
scheduler should launch on its own - a stray restart would burn the OpenDota monthly quota.

    docker compose exec api python -m app.ingestion.cli backfill --pages 5
    docker compose exec api python -m app.ingestion.cli details --limit 200
    docker compose exec api python -m app.ingestion.cli normalize
    docker compose exec api python -m app.ingestion.cli status
"""

import argparse
import asyncio

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.session import dispose_engine, get_session_factory
from app.ingestion.clients.opendota import OpenDotaClient
from app.ingestion.normalize import (
    normalize_match_details,
    normalize_pro_matches,
    normalized_counts,
)
from app.ingestion.repository import count_raw_matches, get_checkpoint
from app.ingestion.sources import Checkpoint, RawSource
from app.ingestion.workers.backfill import run_backfill
from app.ingestion.workers.details import count_missing_details, run_details_backfill

#: ~100 matches per call at 60 req/min, so a page costs about a second of wall clock.
PAGE_SIZE_HINT = 100

#: Measured, not the rate limit. A full match payload is large and OpenDota takes ~3.5s to
#: build it, so detail throughput is bound by response time rather than by the 60 req/min
#: allowance: 150 maps took 9m04s, i.e. about 17 a minute.
DETAIL_FETCH_PER_MINUTE = 16.5


async def cmd_backfill(pages: int, restart: bool) -> None:
    async with OpenDotaClient() as client:
        report = await run_backfill(client, get_session_factory(), pages=pages, restart=restart)

    print(f"pages fetched:   {report.pages}")
    print(f"rows written:    {report.rows}")
    print(f"lowest match_id: {report.lowest_match_id}")
    print(f"stopped because: {report.stopped_because}")


async def cmd_details(limit: int, oldest_first: bool) -> None:
    async with OpenDotaClient() as client:
        report = await run_details_backfill(
            client, get_session_factory(), limit=limit, newest_first=not oldest_first
        )

    print(f"requested: {report.requested}")
    print(f"fetched:   {report.fetched}")
    print(f"failed:    {report.failed}")
    print(f"remaining: {report.remaining}")
    if report.remaining:
        hours = report.remaining / DETAIL_FETCH_PER_MINUTE / 60
        print(f"           (~{hours:.1f} h at ~{DETAIL_FETCH_PER_MINUTE} maps/min)")


async def cmd_normalize(limit: int | None) -> None:
    summaries = await normalize_pro_matches(get_session_factory(), limit=limit)
    details = await normalize_match_details(get_session_factory(), limit=limit)

    print("from /proMatches summaries")
    print(f"  raw rows read: {summaries.raw_seen}")
    print(f"  leagues:       {summaries.leagues}")
    print(f"  teams:         {summaries.teams}")
    print(f"  series:        {summaries.series}")
    print(f"  matches:       {summaries.matches}")
    print("from /matches/{id} details")
    print(f"  raw rows read: {details.raw_seen}")
    print(f"  players:       {details.match_players}")
    print(f"  draft picks:   {details.match_drafts}")
    print(f"  objectives:    {details.match_objectives}")
    for report in (summaries, details):
        if report.skipped:
            print(f"  skipped:       {report.skipped}")


async def cmd_status() -> None:
    async with get_session_factory()() as session:
        cursor = await get_checkpoint(session, Checkpoint.OPENDOTA_PRO_MATCHES)
        summaries = await count_raw_matches(session, RawSource.OPENDOTA_PRO_MATCHES)
        details = await count_raw_matches(session, RawSource.OPENDOTA_MATCH)
        total = await count_raw_matches(session)
        normalized = await normalized_counts(session)
        missing = await count_missing_details(session)

    print(f"checkpoint (oldest match_id seen): {cursor or '-'}")
    print(f"raw pro-match summaries:           {summaries}")
    print(f"raw full match payloads:           {details}")
    print(f"raw rows total:                    {total}")
    print()
    for label, count in normalized.items():
        print(f"{label:<34} {count}")
    print(f"{'maps still missing details':<34} {missing}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="app.ingestion.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    backfill = sub.add_parser("backfill", help="walk /proMatches backwards into raw_matches")
    backfill.add_argument(
        "--pages",
        type=int,
        default=1,
        help=f"pages to fetch, ~{PAGE_SIZE_HINT} matches each (default: 1)",
    )
    backfill.add_argument(
        "--restart",
        action="store_true",
        help="ignore the checkpoint and start from the newest matches again",
    )

    normalize = sub.add_parser(
        "normalize", help="rebuild the normalized layer from stored raw payloads"
    )
    normalize.add_argument("--limit", type=int, default=None, help="stop after N raw rows")

    details = sub.add_parser("details", help="fetch /matches/{id} payloads for maps that lack one")
    details.add_argument(
        "--limit", type=int, default=100, help="maps to fetch, one API call each (default: 100)"
    )
    details.add_argument(
        "--oldest-first",
        action="store_true",
        help="walk history forwards instead of starting from the most recent maps",
    )

    sub.add_parser("status", help="show checkpoint and raw row counts")

    args = parser.parse_args()
    configure_logging(get_settings().log_level)

    async def run() -> None:
        try:
            if args.command == "backfill":
                await cmd_backfill(args.pages, args.restart)
            elif args.command == "details":
                await cmd_details(args.limit, args.oldest_first)
            elif args.command == "normalize":
                await cmd_normalize(args.limit)
            else:
                await cmd_status()
        finally:
            await dispose_engine()

    asyncio.run(run())


if __name__ == "__main__":
    main()
