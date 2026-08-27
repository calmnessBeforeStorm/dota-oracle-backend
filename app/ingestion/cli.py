"""Manual entry point for ingestion runs.

The backfill is a long, interruptible job that a human starts and watches, not something the
scheduler should launch on its own - a stray restart would burn the OpenDota monthly quota.

    docker compose exec api python -m app.ingestion.cli backfill --pages 5
    docker compose exec api python -m app.ingestion.cli details --limit 200
    docker compose exec api python -m app.ingestion.cli normalize
    docker compose exec api python -m app.ingestion.cli link-stages
    docker compose exec api python -m app.ingestion.cli featurize
    docker compose exec api python -m app.ingestion.cli status
"""

import argparse
import asyncio

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.session import dispose_engine, get_session_factory
from app.features.featurize import featurize
from app.features.ratings import rebuild_player_ratings
from app.ingestion.clients.liquipedia import LiquipediaClient
from app.ingestion.clients.opendota import OpenDotaClient
from app.ingestion.liquipedia.sync import (
    refresh_league_meta,
    refresh_stages,
    sync_liquipedia_leagues,
)
from app.ingestion.liquipedia.wikitext import parse_stage_formats
from app.ingestion.normalize import (
    normalize_match_details,
    normalize_pro_matches,
    normalized_counts,
)
from app.ingestion.repository import count_raw_matches, get_checkpoint
from app.ingestion.sources import Checkpoint, RawSource
from app.ingestion.stages import link_series_to_stages
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


async def cmd_liquipedia(page: str) -> None:
    """Show what we can read off a tournament page.

    The tier and format mapping is semi-manual by design (spec section 3), so this exists to
    put the evidence in front of a human before anything is written to the database.
    """
    async with LiquipediaClient() as client:
        wikitext = await client.page_wikitext(page)

    if not wikitext:
        print(f"page not found: {page}")
        return

    stages = parse_stage_formats(wikitext)
    if not stages:
        print("no stage formats found - the page may word its Format section differently")
        return

    print(f"{page}: {len(stages)} stages")
    for stage in stages:
        flag = "  <- mixed formats, confirm by hand" if stage.is_ambiguous else ""
        print(f"  {stage.default_format.value:<4} {stage.stage_type.value:<8} {stage.name}{flag}")
        print(f"       {stage.evidence[:110]}")


async def cmd_refresh_meta() -> None:
    async with LiquipediaClient() as client:
        updated = await refresh_league_meta(client, get_session_factory())
    print(f"leagues updated: {updated}")


async def cmd_refresh_stages(limit: int | None) -> None:
    async with LiquipediaClient() as client:
        written = await refresh_stages(client, get_session_factory(), limit=limit)
    print(f"stages written: {written}")


async def cmd_map_leagues(limit: int | None, apply: bool, escalate: int) -> None:
    """Propose (and optionally apply) league -> Liquipedia mappings.

    A wrong match mislabels every game of a tournament, so nothing is written unless
    --apply is given, and only proposals above the confidence threshold are written at all.
    """
    async with LiquipediaClient() as client:
        report = await sync_liquipedia_leagues(
            client, get_session_factory(), limit=limit, apply=apply, escalate=escalate
        )

    print(f"leagues examined: {report.leagues_seen}")
    print(f"confident:        {report.confident}")
    print(f"applied:          {report.applied}{'' if apply else '  (dry run, pass --apply)'}")
    print(f"stages written:   {report.stages_written}")
    if report.escalated:
        print(
            f"roster lookups:   {report.escalated}"
            f"  (+{report.rescued} resolved, -{report.rejected} ruled out)"
        )

    if report.conflicts:
        print()
        print("CONFLICT: one page claimed by several leagues, none applied:")
        for page, names in report.conflicts.items():
            print(f"  {page}")
            for name in names:
                print(f"    - {name}")

    if report.accepted:
        print()
        print(f"{'applied' if apply else 'would apply'} ({len(report.accepted)}):")
        for proposal in sorted(report.accepted, key=lambda p: -p.score):
            venue = {True: "lan", False: "online", None: "?"}[proposal.is_lan]
            print(f"  {proposal.score:.2f}  {proposal.league_name}")
            print(
                f"        -> {proposal.page_title}"
                f"  [{proposal.tier.value} {venue}] [{proposal.signals}]"
            )

    if report.needs_review:
        print()
        print(f"needs a human ({len(report.needs_review)}):")
        for proposal in sorted(report.needs_review, key=lambda p: -p.score):
            mark = " " if proposal.is_tournament else "!"
            print(f" {mark}{proposal.score:.2f}  {proposal.league_name}")
            print(f"        -> {proposal.page_title}  [{proposal.tier.value}] [{proposal.signals}]")
        print(" ! = the matched page is not a tournament")


async def cmd_link_stages() -> None:
    """Attach series to stages and resolve what that implies. Touches no network."""
    report = await link_series_to_stages(get_session_factory())

    print(f"series seen:        {report.series_seen}")
    print(f"linked to a stage:  {report.linked}")
    print(f"formats set:        {report.formats_set}")
    print(f"conditional flags:  {report.conditional_flags_set}")
    print(f"outcomes resolved:  {report.outcomes_resolved}")
    print(f"draws found:        {report.draws_found}")
    print(f"left ambiguous:     {report.ambiguous}")
    print(f"no stage covers:    {report.no_stage}")
    if report.reasons:
        print()
        for reason, count in sorted(report.reasons.items(), key=lambda kv: -kv[1]):
            print(f"  {count:>5}  {reason}")


async def cmd_featurize(limit: int | None) -> None:
    """Build match_snapshots from stored match payloads. Touches no network."""
    report = await featurize(get_session_factory(), limit=limit)

    print(f"matches seen: {report.matches_seen}")
    print(f"matches used: {report.matches_used}")
    print(f"snapshots:    {report.snapshots}")
    if report.skipped:
        print()
        for reason, count in sorted(report.skipped.items(), key=lambda kv: -kv[1]):
            print(f"  {count:>5}  {reason}")


async def cmd_ratings() -> None:
    """Rebuild point-in-time player ratings. Touches no network."""
    report = await rebuild_player_ratings(get_session_factory())

    print(f"matches processed: {report.matches_processed}")
    print(f"ratings written:   {report.ratings_written}")
    print(f"players rated:     {report.players}")
    if report.skipped:
        print()
        for reason, count in sorted(report.skipped.items(), key=lambda kv: -kv[1]):
            print(f"  {count:>5}  {reason}")


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

    liquipedia = sub.add_parser(
        "liquipedia", help="read series formats off a Liquipedia tournament page"
    )
    liquipedia.add_argument("page", help="page title, e.g. 'The International/2023'")

    mapping = sub.add_parser("map-leagues", help="match leagues to Liquipedia pages and mark tiers")
    mapping.add_argument("--limit", type=int, default=None, help="stop after N leagues")
    mapping.add_argument(
        "--apply", action="store_true", help="persist confident proposals (default: dry run)"
    )
    mapping.add_argument(
        "--escalate",
        type=int,
        default=0,
        metavar="N",
        help=(
            "spend up to N page fetches checking rosters on the closest calls; "
            "each costs 30s of Liquipedia rate limit"
        ),
    )

    sub.add_parser(
        "refresh-meta", help="re-read tier, dates and prize pool of mapped leagues (one request)"
    )

    refresh = sub.add_parser(
        "refresh-stages", help="re-read stages of already mapped leagues (one page per 30s)"
    )
    refresh.add_argument("--limit", type=int, default=None, help="stop after N leagues")

    sub.add_parser(
        "link-stages",
        help="attach series to tournament stages, setting format and series outcome",
    )

    features = sub.add_parser("featurize", help="build match_snapshots from stored parsed matches")
    features.add_argument("--limit", type=int, default=None, help="stop after N matches")

    sub.add_parser("ratings", help="rebuild point-in-time player ratings from every stored match")

    sub.add_parser("status", help="show checkpoint and raw row counts")

    args = parser.parse_args()
    configure_logging(get_settings().log_level)

    async def run() -> None:
        try:
            if args.command == "backfill":
                await cmd_backfill(args.pages, args.restart)
            elif args.command == "details":
                await cmd_details(args.limit, args.oldest_first)
            elif args.command == "map-leagues":
                await cmd_map_leagues(args.limit, args.apply, args.escalate)
            elif args.command == "refresh-meta":
                await cmd_refresh_meta()
            elif args.command == "refresh-stages":
                await cmd_refresh_stages(args.limit)
            elif args.command == "ratings":
                await cmd_ratings()
            elif args.command == "featurize":
                await cmd_featurize(args.limit)
            elif args.command == "link-stages":
                await cmd_link_stages()
            elif args.command == "liquipedia":
                await cmd_liquipedia(args.page)
            elif args.command == "normalize":
                await cmd_normalize(args.limit)
            else:
                await cmd_status()
        finally:
            await dispose_engine()

    asyncio.run(run())


if __name__ == "__main__":
    main()
