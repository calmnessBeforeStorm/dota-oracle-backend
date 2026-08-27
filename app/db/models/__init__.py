"""Import every model module so `Base.metadata` is complete for Alembic autogenerate."""

from app.db.models.enums import LeagueTier, SeriesFormat, StageType
from app.db.models.matches import (
    Match,
    MatchDraft,
    MatchObjective,
    MatchPlayer,
    Series,
)
from app.db.models.raw import IngestCheckpoint, RawLiquipedia, RawLiveSnapshot, RawMatch
from app.db.models.reference import (
    League,
    LeagueMapping,
    Player,
    Team,
    TeamRoster,
    TournamentStage,
)
from app.db.models.training import MatchSnapshot, PlayerRating, Prediction, TeamFeature

__all__ = [
    "IngestCheckpoint",
    "League",
    "LeagueMapping",
    "LeagueTier",
    "Match",
    "MatchDraft",
    "MatchObjective",
    "MatchPlayer",
    "MatchSnapshot",
    "Player",
    "PlayerRating",
    "Prediction",
    "RawLiquipedia",
    "RawLiveSnapshot",
    "RawMatch",
    "Series",
    "SeriesFormat",
    "StageType",
    "Team",
    "TeamFeature",
    "TeamRoster",
    "TournamentStage",
]
