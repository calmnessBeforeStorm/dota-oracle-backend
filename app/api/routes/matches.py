"""F1/F2: live feed and match card (spec section 8.1)."""

import orjson
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redis import get_redis
from app.db.models.matches import Match, Series
from app.db.models.reference import Team
from app.db.models.training import Prediction
from app.db.session import get_session
from app.ingestion.workers.live_poller import LIVE_FEED_KEY
from app.schemas.common import LiveMatch, MatchDetail, PredictionPoint, SeriesBrief, TeamBrief

router = APIRouter(prefix="/matches", tags=["matches"])


@router.get("/live", response_model=list[LiveMatch])
async def live_matches(tier: str | None = None) -> list[LiveMatch]:
    """Whatever the poller last saw.

    Served from the cache the poller writes rather than recomputed, and that cache expires
    after two minutes: an empty feed is honest, a stale one looks live and is not.
    """
    cached = await get_redis().get(LIVE_FEED_KEY)
    if not cached:
        return []

    entries = orjson.loads(cached)
    if tier:
        entries = [entry for entry in entries if entry.get("tier") == tier]
    return [LiveMatch.model_validate(entry) for entry in entries]


@router.get("/{match_id}", response_model=MatchDetail)
async def match_detail(match_id: int, session: AsyncSession = Depends(get_session)) -> MatchDetail:
    """F2: the match card, with the probability curve from the prediction log."""
    curve_rows = (
        await session.execute(
            select(
                Prediction.minute,
                func.max(Prediction.p_radiant),
                func.max(Prediction.predicted_at),
            )
            .where(Prediction.match_id == match_id)
            .group_by(Prediction.minute)
            .order_by(Prediction.minute)
        )
    ).all()

    match = (
        await session.execute(select(Match).where(Match.match_id == match_id))
    ).scalar_one_or_none()

    if match is None and not curve_rows:
        raise HTTPException(status_code=404, detail="match not found")

    curve = [
        PredictionPoint(minute=int(minute), p_radiant=float(p), predicted_at=predicted_at)
        for minute, p, predicted_at in curve_rows
    ]

    if match is None:
        # Live and not yet in the normalized layer: the curve is all we have, and it is the
        # part the card is actually about.
        return MatchDetail(
            match_id=match_id,
            radiant=TeamBrief(),
            dire=TeamBrief(),
            series=SeriesBrief(),
            is_live=True,
            curve=curve,
        )

    name_rows = (
        await session.execute(
            select(Team.team_id, Team.name).where(
                Team.team_id.in_([t for t in (match.radiant_team_id, match.dire_team_id) if t])
            )
        )
    ).all()
    names: dict[int | None, str | None] = {int(team_id): name for team_id, name in name_rows}
    series = (
        await session.execute(select(Series).where(Series.series_id == match.series_id))
    ).scalar_one_or_none()

    return MatchDetail(
        match_id=match_id,
        radiant=TeamBrief(team_id=match.radiant_team_id, name=names.get(match.radiant_team_id)),
        dire=TeamBrief(team_id=match.dire_team_id, name=names.get(match.dire_team_id)),
        series=SeriesBrief(
            series_id=match.series_id,
            format=series.format if series and series.format else None,
            score_a=series.score_a if series else 0,
            score_b=series.score_b if series else 0,
            winner_team_id=series.winner_team_id if series else None,
            is_draw=bool(series.is_draw) if series else False,
            game_in_series=match.game_in_series or 1,
            is_conditional_game=bool(match.is_conditional_game),
        ),
        is_live=match.radiant_win is None,
        radiant_win=match.radiant_win,
        curve=curve,
    )
