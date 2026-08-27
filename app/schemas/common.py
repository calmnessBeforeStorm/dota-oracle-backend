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
    #: None until the stage - and therefore the format - is known (spec section 5.5).
    format: SeriesFormat | None = None
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
    minute: int = 0
    tier: str = "unknown"
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


class TournamentStageInfo(BaseModel):
    """One stage of a tournament (spec section 5.5).

    `default_format` is what makes a Bo2 group stage renderable at all: Valve data cannot
    express Bo2, so without Liquipedia this field would have no source.
    """

    stage_id: int
    name: str
    stage_type: str
    default_format: SeriesFormat | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    series: int = 0


class TournamentSummary(BaseModel):
    """F3: a row in the tournament calendar."""

    league_id: int
    name: str | None = None
    tier: str = "unknown"
    is_lan: bool | None = None
    prize_pool: float | None = None
    liquipedia_slug: str | None = None
    #: Taken from the matches we hold, not from the league row: always available, and it
    #: reflects what was actually played.
    first_match: datetime | None = None
    last_match: datetime | None = None
    maps: int = 0
    stages: int = 0
    status: str = "current"


class TournamentDetail(TournamentSummary):
    """F4: the tournament page."""

    stages: list[TournamentStageInfo] = []  # type: ignore[assignment]
    series_total: int = 0
    series_drawn: int = 0
    #: Series whose stage could not be determined, so their format is still unknown.
    series_without_format: int = 0
