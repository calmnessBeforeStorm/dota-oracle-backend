"""Liquipedia sync: tier marking and stage formats (spec sections 3, 5.5, phase 2).

Two steps, deliberately separate:

  propose  - search Liquipedia for each league, score the candidates, return proposals.
             Reads nothing into the database.
  apply    - persist the decisions that cleared the confidence threshold, recording each in
             `league_mappings` so the history survives a later reclassification.

Rate limits are respected by the client, not by discipline here: Liquipedia bans by IP.
Search costs one request per league at 1 per 2s; the expensive `parse` call, at 1 per 30s,
is only spent on leagues whose mapping was accepted.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.db.models.enums import LeagueTier
from app.db.models.matches import Match
from app.db.models.reference import League, LeagueMapping, TournamentStage
from app.ingestion.liquipedia.matching import (
    LeagueEvidence,
    MappingProposal,
    best_proposal,
)
from app.ingestion.liquipedia.meta import TournamentMeta, parse_meta_description
from app.ingestion.liquipedia.wikitext import parse_stage_formats
from app.ingestion.repository import utcnow

log = get_logger(__name__)

#: Search hits to consider per league. More costs nothing extra - the categories for all of
#: them come back in a single batched request.
CANDIDATES_PER_LEAGUE = 5


class LiquipediaSource(Protocol):
    async def query(self, **params: Any) -> dict[str, Any]: ...
    async def page_wikitext(self, page: str) -> str: ...


@dataclass
class SyncReport:
    leagues_seen: int = 0
    proposed: int = 0
    confident: int = 0
    applied: int = 0
    stages_written: int = 0
    #: Cleared the threshold - applied, or would be with --apply.
    accepted: list[MappingProposal] = field(default_factory=list)
    needs_review: list[MappingProposal] = field(default_factory=list)
    #: Page title -> the leagues that all claimed it. Never applied automatically.
    conflicts: dict[str, list[str]] = field(default_factory=dict)

    def as_log_fields(self) -> dict[str, Any]:
        return {
            "leagues_seen": self.leagues_seen,
            "proposed": self.proposed,
            "confident": self.confident,
            "applied": self.applied,
            "stages_written": self.stages_written,
            "needs_review": len(self.needs_review),
            "conflicts": len(self.conflicts),
        }


async def _search_titles(client: LiquipediaSource, name: str) -> list[str]:
    response = await client.query(list="search", srsearch=name, srlimit=CANDIDATES_PER_LEAGUE)
    return [hit["title"] for hit in (response.get("query") or {}).get("search") or []]


async def _page_facts(
    client: LiquipediaSource, titles: list[str]
) -> dict[str, tuple[list[str], TournamentMeta | None]]:
    """Categories and generated description for several pages, in one request.

    Both properties come back together, so the dates, tier, team count and prize pool cost
    nothing beyond the category lookup that was already being made.
    """
    if not titles:
        return {}
    response = await client.query(prop="categories|pageprops", titles="|".join(titles), cllimit=500)
    pages = (response.get("query") or {}).get("pages") or {}

    facts: dict[str, tuple[list[str], TournamentMeta | None]] = {}
    for page in pages.values():
        title = page.get("title")
        if not title:
            continue
        categories = [c["title"] for c in page.get("categories") or []]
        props = page.get("pageprops") or {}
        description = props.get("metadescl")
        meta = (
            parse_meta_description(description, props.get("displaytitle")) if description else None
        )
        facts[title] = (categories, meta)
    return facts


async def league_evidence(session: AsyncSession) -> dict[int, LeagueEvidence]:
    """What our own matches say about each league: when it ran and how many teams played.

    Independent of anything Liquipedia claims, which is the point - it is what turns a name
    guess into a corroborated one.
    """
    rows = (
        await session.execute(
            select(
                Match.league_id,
                func.min(Match.start_time),
                func.max(Match.start_time),
                func.count(func.distinct(Match.radiant_team_id)),
            )
            .where(Match.league_id.is_not(None))
            .group_by(Match.league_id)
        )
    ).all()

    evidence: dict[int, LeagueEvidence] = {}
    for league_id, first, last, radiant_teams in rows:
        evidence[int(league_id)] = LeagueEvidence(
            first_match=first.date() if first else None,
            last_match=last.date() if last else None,
            # Counting one side only would halve it; every team plays both sides across a
            # tournament, so distinct radiant teams is already the field size in practice.
            team_count=int(radiant_teams) or None,
        )
    return evidence


async def propose_mappings(
    client: LiquipediaSource,
    session_factory: async_sessionmaker[AsyncSession],
    limit: int | None = None,
    only_unmapped: bool = True,
) -> list[MappingProposal]:
    """Score a Liquipedia page for each league. Writes nothing."""
    async with session_factory() as session:
        statement = select(League.league_id, League.name).order_by(League.league_id)
        if only_unmapped:
            statement = statement.where(League.liquipedia_slug.is_(None))
        if limit is not None:
            statement = statement.limit(limit)
        leagues = list((await session.execute(statement)).all())

    async with session_factory() as session:
        evidence = await league_evidence(session)

    proposals: list[MappingProposal] = []
    for league_id, name in leagues:
        if not name:
            continue
        titles = await _search_titles(client, name)
        facts = await _page_facts(client, titles)
        candidates = [(title, *facts.get(title, ([], None))) for title in titles]

        proposal = best_proposal(league_id, name, candidates, evidence.get(league_id))
        if proposal is not None:
            proposals.append(proposal)
            log.info(
                "liquipedia.proposal",
                league_id=league_id,
                name=name,
                page=proposal.page_title,
                score=round(proposal.score, 3),
                tier=proposal.tier.value,
                signals=proposal.signals,
                confident=proposal.is_confident,
            )
    return proposals


async def apply_mapping(
    session: AsyncSession,
    proposal: MappingProposal,
    decided_by: str = "auto",
    note: str | None = None,
    now: datetime | None = None,
) -> None:
    """Record a decision and make it the active one.

    The previous decision is superseded rather than overwritten: a tournament can be
    reclassified, and why a match counted as Tier 1 last month has to stay answerable.
    """
    timestamp = now or utcnow()

    await session.execute(
        update(LeagueMapping)
        .where(
            LeagueMapping.league_id == proposal.league_id,
            LeagueMapping.superseded_at.is_(None),
        )
        .values(superseded_at=timestamp)
    )
    session.add(
        LeagueMapping(
            league_id=proposal.league_id,
            liquipedia_slug=proposal.page_title,
            tier=proposal.tier.value,
            is_lan=proposal.is_lan,
            score=proposal.score,
            decided_by=decided_by,
            decided_at=timestamp,
            note=note,
        )
    )
    # Denormalized onto the league for querying; league_mappings stays the record.
    await session.execute(
        update(League)
        .where(League.league_id == proposal.league_id)
        .values(
            liquipedia_slug=proposal.page_title,
            tier=proposal.tier.value,
            is_lan=proposal.is_lan,
            updated_at=timestamp,
        )
    )


async def sync_stage_formats(
    client: LiquipediaSource,
    session: AsyncSession,
    league_id: int,
    page_title: str,
) -> int:
    """Read the stages of one tournament page into `tournament_stages`.

    This is the source of truth for series formats, Bo2 included (spec section 5.5). Stages
    the page does not state a format for are not written: unknown stays unknown.
    """
    wikitext = await client.page_wikitext(page_title)
    stages = parse_stage_formats(wikitext)
    if not stages:
        return 0

    now = utcnow()
    rows = [
        {
            "league_id": league_id,
            "name": stage.name,
            "stage_type": stage.stage_type.value,
            "default_format": stage.default_format.value,
            "liquipedia_slug": page_title,
            "created_at": now,
            "updated_at": now,
        }
        for stage in stages
    ]

    # Stage names are stable within a tournament page, so re-reading it updates in place.
    statement = insert(TournamentStage).values(rows)
    statement = statement.on_conflict_do_update(
        constraint="uq_tournament_stages_league_name",
        set_={
            "stage_type": statement.excluded.stage_type,
            "default_format": statement.excluded.default_format,
            "liquipedia_slug": statement.excluded.liquipedia_slug,
            "updated_at": statement.excluded.updated_at,
        },
    )
    await session.execute(statement)
    return len(rows)


async def sync_liquipedia_leagues(
    client: LiquipediaSource,
    session_factory: async_sessionmaker[AsyncSession],
    limit: int | None = None,
    apply: bool = False,
    with_stages: bool = True,
) -> SyncReport:
    """Full pass: propose, then optionally persist the confident ones and their stages."""
    report = SyncReport()
    proposals = await propose_mappings(client, session_factory, limit=limit)
    report.leagues_seen = len(proposals)
    report.proposed = len(proposals)

    # A Liquipedia page describes exactly one tournament, so two leagues claiming the same
    # page means at least one of them is wrong. Applying either would be a coin flip.
    claims: dict[str, list[MappingProposal]] = {}
    for proposal in proposals:
        if proposal.is_confident:
            claims.setdefault(proposal.page_title, []).append(proposal)
    contested = {page for page, group in claims.items() if len(group) > 1}
    report.conflicts = {page: [p.league_name for p in claims[page]] for page in sorted(contested)}

    for proposal in proposals:
        if not proposal.is_confident or proposal.page_title in contested:
            report.needs_review.append(proposal)
            continue
        report.confident += 1
        report.accepted.append(proposal)

        if not apply:
            continue

        async with session_factory() as session:
            await apply_mapping(session, proposal)
            if with_stages and proposal.tier is not LeagueTier.UNKNOWN:
                report.stages_written += await sync_stage_formats(
                    client, session, proposal.league_id, proposal.page_title
                )
            await session.commit()
        report.applied += 1

    log.info("liquipedia.sync_done", **report.as_log_fields())
    return report
