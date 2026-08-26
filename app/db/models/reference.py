"""Reference layer: leagues, stages, teams, players, rosters (spec section 4.2)."""

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.db.models.enums import LeagueTier, SeriesFormat, StageType


class League(Base, TimestampMixin):
    """Tier mapping is versioned and semi-manual (spec section 3)."""

    __tablename__ = "leagues"

    league_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # Steam/OpenDota league id
    name: Mapped[str | None] = mapped_column(String(255))
    liquipedia_slug: Mapped[str | None] = mapped_column(String(255), index=True)
    tier: Mapped[str] = mapped_column(String(16), default=LeagueTier.UNKNOWN, nullable=False)
    is_lan: Mapped[bool | None] = mapped_column(Boolean)
    prize_pool: Mapped[float | None] = mapped_column(Numeric(12, 2))
    organizer: Mapped[str | None] = mapped_column(String(255))
    region: Mapped[str | None] = mapped_column(String(64))
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)


class TournamentStage(Base, TimestampMixin):
    """Source of truth for series format (spec section 5.5).

    `points_rule` is configuration, never hardcoded: Bo2 scoring differs per tournament
    (typical DPC: 3 points for 2-0, 1 each for 1-1, 0 for 0-2 - but not universal).
    """

    __tablename__ = "tournament_stages"

    stage_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    league_id: Mapped[int] = mapped_column(
        ForeignKey("leagues.league_id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    stage_type: Mapped[str] = mapped_column(String(16), default=StageType.GROUP, nullable=False)
    default_format: Mapped[str] = mapped_column(String(8), default=SeriesFormat.BO3, nullable=False)
    points_rule: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    liquipedia_slug: Mapped[str | None] = mapped_column(String(255))
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Team(Base, TimestampMixin):
    __tablename__ = "teams"

    team_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str | None] = mapped_column(String(255))
    tag: Mapped[str | None] = mapped_column(String(64))
    logo_url: Mapped[str | None] = mapped_column(String(512))


class Player(Base, TimestampMixin):
    __tablename__ = "players"

    account_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str | None] = mapped_column(String(255))
    country: Mapped[str | None] = mapped_column(String(8))
    fantasy_role: Mapped[int | None] = mapped_column(Integer)


class TeamRoster(Base, TimestampMixin):
    """Roster history. Needed for point-in-time features and stand-in detection."""

    __tablename__ = "team_rosters"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    team_id: Mapped[int] = mapped_column(
        ForeignKey("teams.team_id", ondelete="CASCADE"), nullable=False, index=True
    )
    account_id: Mapped[int] = mapped_column(
        ForeignKey("players.account_id", ondelete="CASCADE"), nullable=False, index=True
    )
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_standin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
