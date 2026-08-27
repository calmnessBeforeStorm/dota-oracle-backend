"""Reference layer: leagues, stages, teams, players, rosters (spec section 4.2)."""

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
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
    __table_args__ = (
        # Stage names are stable within a tournament page, so re-reading it updates in
        # place instead of piling up duplicates on every sync.
        UniqueConstraint("league_id", "name", name="uq_tournament_stages_league_name"),
    )

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


class Hero(Base, TimestampMixin):
    """Hero constants (spec section 2.2/A3).

    `match_drafts` and `match_players` store bare hero ids, so without this table a match
    card can only show numbers. Kept server-side rather than as a static file in the SPA for
    two reasons: the draft sub-model of section 6.3 will need heroes on the server anyway,
    and a shipped file goes stale silently the next time Valve adds a hero.

    Loaded from `/constants/heroes`, which costs no quota worth counting - one call for all
    127 of them, and the same data is mirrored in the odota/dotaconstants repository.
    """

    __tablename__ = "heroes"

    hero_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: `npc_dota_hero_antimage` - the internal name, stable across renames of the display one.
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    localized_name: Mapped[str] = mapped_column(String(64), nullable=False)
    primary_attr: Mapped[str | None] = mapped_column(String(8))
    attack_type: Mapped[str | None] = mapped_column(String(16))
    roles: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    #: Path under Valve's CDN, as OpenDota reports it. Stored as given rather than expanded
    #: into a full URL: the host has changed before, and the join is the caller's business.
    image_path: Mapped[str | None] = mapped_column(String(255))


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


class LeagueMapping(Base):
    """History of tier/Liquipedia decisions for a league (spec section 3, point 4).

    The mapping is versioned on purpose: a tournament can be reclassified, and the reason a
    match was treated as Tier 1 six months ago has to remain answerable. `leagues` carries
    the currently active decision denormalized for queries; this table is the record.

    A row with `superseded_at IS NULL` is the active decision.
    """

    __tablename__ = "league_mappings"
    __table_args__ = (Index("ix_league_mappings_active", "league_id", "superseded_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    league_id: Mapped[int] = mapped_column(
        ForeignKey("leagues.league_id", ondelete="CASCADE"), nullable=False
    )
    liquipedia_slug: Mapped[str | None] = mapped_column(String(255))
    tier: Mapped[str] = mapped_column(String(16), nullable=False)
    is_lan: Mapped[bool | None] = mapped_column(Boolean)
    #: Name-similarity score the proposal was made with, 0..1. NULL for a human decision.
    score: Mapped[float | None] = mapped_column(Float)
    #: "auto" when accepted above the confidence threshold, "manual" when a human decided.
    decided_by: Mapped[str] = mapped_column(String(16), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(String(512))
