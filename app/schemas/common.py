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


class MatchPlayerBrief(BaseModel):
    """One player on the match card (F2).

    `player_name` is null for 13% of rows: `/proPlayers` only lists players with a pro
    profile, and a stand-in may not have one. Null is left as null rather than filled with
    the account id, which reads as a name and is not one.
    """

    player_slot: int
    is_radiant: bool
    hero_id: int | None = None
    hero_name: str | None = None
    hero_image: str | None = None
    account_id: int | None = None
    player_name: str | None = None
    kills: int | None = None
    deaths: int | None = None
    assists: int | None = None
    last_hits: int | None = None
    denies: int | None = None
    net_worth: int | None = None
    gold_per_min: int | None = None
    xp_per_min: int | None = None


class DraftEntry(BaseModel):
    """One pick or ban, in the order it happened (F2)."""

    order: int
    is_pick: bool
    is_radiant: bool
    hero_id: int
    hero_name: str | None = None
    hero_image: str | None = None


class TimelineEvent(BaseModel):
    """A key moment, in a form the UI can label without re-parsing Valve's strings.

    The raw log stores `building_kill` plus an npc name, and chat events as
    `CHAT_MESSAGE_*`. Both are decoded here so the client renders meaning rather than
    guessing at vocabulary that belongs to the ingestion layer.
    """

    time: int  # seconds from the horn; negative before it
    minute: int
    kind: str  # tower | barracks | ancient | roshan | aegis | first_blood | tormentor
    #: For a building, the side that LOST it. For roshan and first blood, the side that did
    #: it. Null when the log does not say.
    is_radiant: bool | None = None
    lane: str | None = None


class MatchDetail(BaseModel):
    """F2: match card with the probability curve, rosters, draft and timeline."""

    match_id: int
    radiant: TeamBrief
    dire: TeamBrief
    series: SeriesBrief
    is_live: bool
    radiant_win: bool | None = None
    curve: list[PredictionPoint] = []
    players: list[MatchPlayerBrief] = []
    draft: list[DraftEntry] = []
    timeline: list[TimelineEvent] = []


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


class SeriesResult(BaseModel):
    """One series on the tournament page (F4).

    `winner_team_id` nullable and `is_draw` explicit, as everywhere: a Bo2 ending 1-1 and a
    series still being played are different states (spec section 5.5).
    """

    series_id: int
    stage_id: int | None = None
    format: SeriesFormat | None = None
    team_a: TeamBrief
    team_b: TeamBrief
    score_a: int = 0
    score_b: int = 0
    winner_team_id: int | None = None
    is_draw: bool = False
    #: When the first map was played. Null for a series whose maps we do not hold.
    played_at: datetime | None = None
    maps: int = 0


class TournamentParticipant(BaseModel):
    """A team's record in one tournament (F4), derived from its series."""

    team: TeamBrief
    series_won: int = 0
    series_lost: int = 0
    series_drawn: int = 0
    maps_won: int = 0
    maps_lost: int = 0


class TournamentDetail(TournamentSummary):
    """F4: the tournament page.

    No bracket. Our series carry no round and no "winner of this plays winner of that", and
    Dota playoffs are almost always double elimination, so upper and lower bracket cannot be
    told apart from dates and results. Drawing one would be a guess presented as a fact; it
    needs Liquipedia's bracket templates, which is separate work.
    """

    stages: list[TournamentStageInfo] = []  # type: ignore[assignment]
    series_total: int = 0
    series_drawn: int = 0
    #: Series whose stage could not be determined, so their format is still unknown.
    series_without_format: int = 0
    participants: list[TournamentParticipant] = []
    results: list[SeriesResult] = []
