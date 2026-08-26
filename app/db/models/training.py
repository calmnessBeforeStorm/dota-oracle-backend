"""Training layer (spec section 4.3). Every split is by match_id, never by row."""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class MatchSnapshot(Base):
    """Core of the live dataset: one row = (match_id, minute) + state features + label."""

    __tablename__ = "match_snapshots"

    match_id: Mapped[int] = mapped_column(
        ForeignKey("matches.match_id", ondelete="CASCADE"), primary_key=True
    )
    minute: Mapped[int] = mapped_column(Integer, primary_key=True)
    features: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    radiant_win: Mapped[bool] = mapped_column(Boolean, nullable=False)  # label, duplicated


class PlayerRating(Base):
    """Point-in-time ratings. Anything else leaks the future into features."""

    __tablename__ = "player_ratings"
    __table_args__ = (Index("ix_player_ratings_account_time", "account_id", "as_of_time"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    as_of_match_id: Mapped[int | None] = mapped_column(BigInteger)
    as_of_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    mu: Mapped[float] = mapped_column(Float, nullable=False)
    sigma: Mapped[float] = mapped_column(Float, nullable=False)
    games: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class TeamFeature(Base):
    __tablename__ = "team_features"
    __table_args__ = (Index("ix_team_features_team_time", "team_id", "as_of_time"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    team_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    as_of_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    features: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class Prediction(Base):
    """Log of every prediction ever served. Not optional: without it, a quality drop
    a month from now is undiagnosable (spec section 4.3)."""

    __tablename__ = "predictions"
    __table_args__ = (Index("ix_predictions_match_minute", "match_id", "minute"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    match_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    minute: Mapped[int] = mapped_column(Integer, nullable=False)
    predicted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    model_version: Mapped[str] = mapped_column(String(64), nullable=False)
    p_radiant: Mapped[float] = mapped_column(Float, nullable=False)
    features: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
