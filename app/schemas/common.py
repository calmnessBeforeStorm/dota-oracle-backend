"""API schemas (spec sections 8.1, 5.5)."""

from datetime import datetime

from pydantic import BaseModel, Field

from app.db.models.enums import SeriesFormat


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str
    env: str


class TeamBrief(BaseModel):
    team_id: int | None = None
    name: str | None = None
    logo_url: str | None = None


class SeriesBrief(BaseModel):
    """Series context for a match card.

    `winner_team_id` is nullable and `is_draw` is explicit: a Bo2 can end 1-1, and the UI
    must render that as a draw rather than as "no result yet" (spec section 5.5).
    """

    series_id: int | None = None
    format: SeriesFormat = SeriesFormat.BO1
    score_a: int = 0
    score_b: int = 0
    winner_team_id: int | None = None
    is_draw: bool = False
    game_in_series: int = 1
    is_conditional_game: bool = False


class LiveMatch(BaseModel):
    """F1: a card in the live feed."""

    match_id: int
    league_id: int | None = None
    league_name: str | None = None
    radiant: TeamBrief
    dire: TeamBrief
    game_time: int = Field(description="Seconds since the game started")
    radiant_score: int = 0
    dire_score: int = 0
    p_radiant: float = Field(ge=0.0, le=1.0)
    model_version: str
    series: SeriesBrief
    stream_delay_s: int = Field(
        default=0,
        description=(
            "Broadcast delay reported by Valve. The UI must show this: our numbers are "
            "ahead of what the viewer sees, otherwise it reads as spoiling the match "
            "(spec section 7.4)."
        ),
    )


class PredictionPoint(BaseModel):
    minute: int
    p_radiant: float = Field(ge=0.0, le=1.0)
    predicted_at: datetime


class MatchDetail(BaseModel):
    """F2: match card with the probability curve."""

    match_id: int
    radiant: TeamBrief
    dire: TeamBrief
    series: SeriesBrief
    is_live: bool
    radiant_win: bool | None = None
    curve: list[PredictionPoint] = []


class ModelMetrics(BaseModel):
    """F6: public calibration dashboard. Averages lie - always report per minute bucket."""

    model_version: str
    log_loss_by_minute: dict[str, float]
    brier_by_minute: dict[str, float]
    ece: float
    sample_size: int
