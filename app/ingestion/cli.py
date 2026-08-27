"""Manual entry point for ingestion runs.

The backfill is a long, interruptible job that a human starts and watches, not something the
scheduler should launch on its own - a stray restart would burn the OpenDota monthly quota.

    docker compose exec api python -m app.ingestion.cli backfill --pages 5
    docker compose exec api python -m app.ingestion.cli status
"""

import argparse
import asyncio

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.session import dispose_engine, get_session_factory
from app.ingestion.clients.opendota import OpenDotaClient
from app.ingestion.repository import count_raw_matches, get_checkpoint
from app.ingestion.sources import Checkpoint, RawSource
from app.ingestion.workers.backfill import run_backfill

#: ~100 matches per call at 60 req/min, so a page costs about a second of wall clock.
PAGE_SIZE_HINT = 100


async def cmd_backfill(pages: int, restart: bool) -> None:
    async with OpenDotaClient() as client:
        report = await run_backfill(client, get_session_factory(), pages=pages, restart=restart)

    print(f"pages fetched:   {report.pages}")
    print(f"rows written:    {report.rows}")
    print(f"lowest match_id: {report.lowest_match_id}")
    print(f"stopped because: {report.stopped_because}")


async def cmd_status() -> None:
    async with get_session_factory()() as session:
        cursor = await get_checkpoint(session, Checkpoint.OPENDOTA_PRO_MATCHES)
        summaries = await count_raw_matches(session, RawSource.OPENDOTA_PRO_MATCHES)
        details = await count_raw_matches(session, RawSource.OPENDOTA_MATCH)
        total = await count_raw_matches(session)

    print(f"checkpoint (oldest match_id seen): {cursor or '-'}")
    print(f"raw pro-match summaries:           {summaries}")
    print(f"raw full match payloads:           {details}")
    print(f"raw rows total:                    {total}")


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

    sub.add_parser("status", help="show checkpoint and raw row counts")

    args = parser.parse_args()
    configure_logging(get_settings().log_level)

    async def run() -> None:
        try:
            if args.command == "backfill":
                await cmd_backfill(args.pages, args.restart)
            else:
                await cmd_status()
        finally:
            await dispose_engine()

    asyncio.run(run())


if __name__ == "__main__":
    main()
