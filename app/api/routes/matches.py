"""F1/F2: live feed and match card (spec section 8.1)."""

import orjson
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.card import draft_for, players_for, timeline_for
from app.api.live_card import (
    draft_from,
    kill_score,
    latest_snapshot,
    map_score,
    players_from,
    series_from,
    stream_delay_seconds,
    teams_from,
)
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

    # A live match has no detail payload yet, so the normalized tables hold nothing for it.
    # The poller's own snapshots do: teams, the kill score, both rosters and the draft.
    live = None
    if match is None or match.radiant_win is None:
        live = await latest_snapshot(session, match_id)

    if match is None:
        if live is None:
            # Predictions but no snapshot: the poller ran before it began storing them, or
            # the rows were pruned. The curve is genuinely all there is.
            return MatchDetail(
                match_id=match_id,
                radiant=TeamBrief(),
                dire=TeamBrief(),
                series=SeriesBrief(),
                is_live=True,
                curve=curve,
            )

        radiant_team, dire_team = teams_from(live)
        live_radiant_kills, live_dire_kills = kill_score(live)
        return MatchDetail(
            match_id=match_id,
            radiant=radiant_team,
            dire=dire_team,
            # No format badge: it comes from the Liquipedia stage, and `series_type` cannot
            # express Bo2 so it is not a substitute (spec section 5.5).
            series=series_from(live, series_format=None),
            is_live=True,
            radiant_score=live_radiant_kills,
            dire_score=live_dire_kills,
            stream_delay_seconds=stream_delay_seconds(live),
            curve=curve,
            players=await players_from(session, live),
            draft=await draft_from(session, live),
        )

    name_rows = (
        await session.execute(
            select(Team.team_id, Team.name).where(
                Team.team_id.in_([t for t in (match.radiant_team_id, match.dire_team_id) if t])
            )
        )
    ).all()
    names: dict[int | None, str | None] = {int(team_id): name for team_id, name in name_rows}

    radiant_team = TeamBrief(team_id=match.radiant_team_id, name=names.get(match.radiant_team_id))
    dire_team = TeamBrief(team_id=match.dire_team_id, name=names.get(match.dire_team_id))
    if not (radiant_team.name or dire_team.name):
        # Team names arrive with the /proMatches summary. A match first learned about
        # through `resolve-outcomes` has team ids but no `teams` rows, so it would sit under
        # "Radiant - Dire" until the summary caught up - while the poller's own record of
        # the same game names both sides.
        live = live or await latest_snapshot(session, match_id)
        if live:
            radiant_team, dire_team = teams_from(live)
    series = (
        await session.execute(select(Series).where(Series.series_id == match.series_id))
    ).scalar_one_or_none()

    # Normalized first, live only where it is silent. A parsed replay beats a scoreboard
    # sampled mid-fight, so the live snapshot never overwrites what is already known.
    players = await players_for(session, match_id)
    draft = await draft_for(session, match_id)
    radiant_kills, dire_kills = map_score(players, live, is_live=match.radiant_win is None)

    return MatchDetail(
        match_id=match_id,
        radiant=radiant_team,
        dire=dire_team,
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
        radiant_score=radiant_kills,
        dire_score=dire_kills,
        # Only while it is running: a finished match has no broadcast to be ahead of, and
        # the snapshot's delay is a fact about a stream that has ended.
        stream_delay_seconds=(
            stream_delay_seconds(live) if live and match.radiant_win is None else 0
        ),
        curve=curve,
        players=players or (await players_from(session, live) if live else []),
        draft=draft or (await draft_from(session, live) if live else []),
        timeline=await timeline_for(session, match_id),
    )
