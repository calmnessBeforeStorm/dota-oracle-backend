"""F3/F4: tournament calendar and page (spec section 8.1).

Any page rendering Liquipedia-derived data must carry visible CC-BY-SA attribution
(spec section 13).
"""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.tournament import participants_from, series_for
from app.db.models.matches import Match, Series
from app.db.models.reference import League, TournamentStage
from app.db.session import get_session
from app.schemas.common import (
    TournamentDetail,
    TournamentStageInfo,
    TournamentSummary,
)

router = APIRouter(prefix="/tournaments", tags=["tournaments"])


def _status_of(start: datetime | None, end: datetime | None, now: datetime) -> str:
    if start and start > now:
        return "upcoming"
    if end and end < now:
        return "past"
    return "current"


@router.get("", response_model=list[TournamentSummary])
async def list_tournaments(
    status: str = Query(default="current", pattern="^(current|upcoming|past|all)$"),
    tier: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> list[TournamentSummary]:
    """Tournaments we hold matches for.

    Dates come from the matches themselves rather than from the league row: they are always
    available and reflect what was actually played, while the Liquipedia-sourced fields are
    only present for leagues whose mapping has been accepted.
    """
    played = (
        select(
            Match.league_id.label("league_id"),
            func.min(Match.start_time).label("first_match"),
            func.max(Match.start_time).label("last_match"),
            func.count(Match.match_id).label("maps"),
        )
        .where(Match.league_id.is_not(None))
        .group_by(Match.league_id)
        .subquery()
    )
    stage_counts = (
        select(
            TournamentStage.league_id.label("league_id"),
            func.count(TournamentStage.stage_id).label("stages"),
        )
        .group_by(TournamentStage.league_id)
        .subquery()
    )

    statement = (
        select(League, played, func.coalesce(stage_counts.c.stages, 0))
        .join(played, played.c.league_id == League.league_id)
        .join(stage_counts, stage_counts.c.league_id == League.league_id, isouter=True)
        .order_by(played.c.last_match.desc())
    )
    if tier:
        statement = statement.where(League.tier == tier)

    now = datetime.now(UTC)
    rows = (await session.execute(statement)).all()

    tournaments = [
        TournamentSummary(
            league_id=league.league_id,
            name=league.name,
            tier=league.tier,
            is_lan=league.is_lan,
            prize_pool=float(league.prize_pool) if league.prize_pool is not None else None,
            liquipedia_slug=league.liquipedia_slug,
            first_match=first_match,
            last_match=last_match,
            maps=maps,
            stages=stages,
            status=_status_of(first_match, last_match, now),
        )
        for league, _, first_match, last_match, maps, stages in rows
    ]

    if status != "all":
        tournaments = [t for t in tournaments if t.status == status]
    return tournaments


@router.get("/{league_id}", response_model=TournamentDetail)
async def tournament_detail(
    league_id: int, session: AsyncSession = Depends(get_session)
) -> TournamentDetail:
    """Stages, formats and series counts for one tournament.

    Stages carry `default_format`, so a Bo2 group stage renders as a Bo2 rather than being
    guessed at from Valve data, which cannot express it (spec section 5.5).
    """
    league = (
        await session.execute(select(League).where(League.league_id == league_id))
    ).scalar_one_or_none()
    if league is None:
        raise HTTPException(status_code=404, detail="tournament not found")

    stage_rows_counted = (
        await session.execute(
            select(Series.stage_id, func.count(Series.series_id))
            .where(Series.league_id == league_id, Series.stage_id.is_not(None))
            .group_by(Series.stage_id)
        )
    ).all()
    series_per_stage: dict[int, int] = {
        int(stage_id): int(count) for stage_id, count in stage_rows_counted if stage_id
    }

    stage_rows = (
        await session.execute(
            select(TournamentStage)
            .where(TournamentStage.league_id == league_id)
            .order_by(TournamentStage.starts_at.nulls_last(), TournamentStage.name)
        )
    ).scalars()

    stages = [
        TournamentStageInfo(
            stage_id=stage.stage_id,
            name=stage.name,
            stage_type=stage.stage_type,
            default_format=stage.default_format,
            starts_at=stage.starts_at,
            ends_at=stage.ends_at,
            series=series_per_stage.get(stage.stage_id, 0),
        )
        for stage in stage_rows
    ]

    totals = (
        await session.execute(
            select(
                func.min(Match.start_time),
                func.max(Match.start_time),
                func.count(Match.match_id),
            ).where(Match.league_id == league_id)
        )
    ).one()
    first_match, last_match, maps = totals

    series_total, drawn, undecided = (
        await session.execute(
            select(
                func.count(Series.series_id),
                func.count(Series.series_id).filter(Series.is_draw.is_(True)),
                func.count(Series.series_id).filter(Series.format.is_(None)),
            ).where(Series.league_id == league_id)
        )
    ).one()

    results = await series_for(session, league_id)

    return TournamentDetail(
        league_id=league.league_id,
        name=league.name,
        tier=league.tier,
        is_lan=league.is_lan,
        prize_pool=float(league.prize_pool) if league.prize_pool is not None else None,
        liquipedia_slug=league.liquipedia_slug,
        first_match=first_match,
        last_match=last_match,
        maps=maps,
        stages=stages,
        status=_status_of(first_match, last_match, datetime.now(UTC)),
        series_total=series_total,
        series_drawn=drawn,
        series_without_format=undecided,
        participants=participants_from(results),
        results=results,
    )
