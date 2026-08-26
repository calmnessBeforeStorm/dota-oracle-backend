"""Normalized match layer (spec section 4.2)."""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.db.models.enums import SeriesFormat


class Series(Base, TimestampMixin):
    """A Bo1/Bo2/Bo3/Bo5 between two teams (spec section 5.5).

    `winner_team_id` is NULLABLE on purpose: a Bo2 can end 1-1. `is_draw` marks that case
    explicitly, so "no winner yet" and "drew" are never confused.

    `format` lives on the series itself, not only on the stage: individual series deviate
    from the stage default (replays, tiebreakers).
    """

    __tablename__ = "series"
    __table_args__ = (
        CheckConstraint(
            "NOT (is_draw AND winner_team_id IS NOT NULL)",
            name="draw_has_no_winner",
        ),
    )

    series_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    league_id: Mapped[int | None] = mapped_column(
        ForeignKey("leagues.league_id", ondelete="SET NULL"), index=True
    )
    stage_id: Mapped[int | None] = mapped_column(
        ForeignKey("tournament_stages.stage_id", ondelete="SET NULL"), index=True
    )
    team_a_id: Mapped[int | None] = mapped_column(ForeignKey("teams.team_id", ondelete="SET NULL"))
    team_b_id: Mapped[int | None] = mapped_column(ForeignKey("teams.team_id", ondelete="SET NULL"))
    format: Mapped[str] = mapped_column(String(8), default=SeriesFormat.BO3, nullable=False)
    score_a: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    score_b: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    winner_team_id: Mapped[int | None] = mapped_column(BigInteger)
    is_draw: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Raw Valve hint only - unreliable, never the source of truth (spec section 2.2/A1).
    valve_series_type: Mapped[int | None] = mapped_column(Integer)


class Match(Base, TimestampMixin):
    """One map. The unit of prediction - a map always has a winner."""

    __tablename__ = "matches"
    __table_args__ = (Index("ix_matches_league_start", "league_id", "start_time"),)

    match_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    league_id: Mapped[int | None] = mapped_column(
        ForeignKey("leagues.league_id", ondelete="SET NULL"), index=True
    )
    series_id: Mapped[int | None] = mapped_column(
        ForeignKey("series.series_id", ondelete="SET NULL"), index=True
    )
    game_in_series: Mapped[int | None] = mapped_column(Integer)
    # True when this map was played only because of the series score (Bo3 game 3, Bo5 games 4-5).
    # Feed it alongside game_in_series or the model learns the format artifact (spec section 5.5).
    is_conditional_game: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    radiant_team_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    dire_team_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    radiant_win: Mapped[bool | None] = mapped_column(Boolean)
    start_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    duration: Mapped[int | None] = mapped_column(Integer)  # seconds
    is_parsed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    patch: Mapped[int | None] = mapped_column(Integer)


class MatchPlayer(Base):
    __tablename__ = "match_players"

    match_id: Mapped[int] = mapped_column(
        ForeignKey("matches.match_id", ondelete="CASCADE"), primary_key=True
    )
    player_slot: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    hero_id: Mapped[int | None] = mapped_column(Integer)
    is_radiant: Mapped[bool] = mapped_column(Boolean, nullable=False)
    lane_role: Mapped[int | None] = mapped_column(Integer)
    kills: Mapped[int | None] = mapped_column(Integer)
    deaths: Mapped[int | None] = mapped_column(Integer)
    assists: Mapped[int | None] = mapped_column(Integer)
    last_hits: Mapped[int | None] = mapped_column(Integer)
    denies: Mapped[int | None] = mapped_column(Integer)
    net_worth: Mapped[int | None] = mapped_column(Integer)
    gold_per_min: Mapped[int | None] = mapped_column(Integer)
    xp_per_min: Mapped[int | None] = mapped_column(Integer)
    leaver_status: Mapped[int | None] = mapped_column(Integer)
    is_standin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class MatchDraft(Base):
    __tablename__ = "match_drafts"

    match_id: Mapped[int] = mapped_column(
        ForeignKey("matches.match_id", ondelete="CASCADE"), primary_key=True
    )
    order: Mapped[int] = mapped_column(Integer, primary_key=True)
    is_pick: Mapped[bool] = mapped_column(Boolean, nullable=False)
    hero_id: Mapped[int] = mapped_column(Integer, nullable=False)
    team: Mapped[int] = mapped_column(Integer, nullable=False)  # 0 = radiant, 1 = dire


class MatchObjective(Base):
    __tablename__ = "match_objectives"
    __table_args__ = (Index("ix_match_objectives_match_time", "match_id", "time"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    match_id: Mapped[int] = mapped_column(
        ForeignKey("matches.match_id", ondelete="CASCADE"), nullable=False
    )
    time: Mapped[int] = mapped_column(Integer, nullable=False)  # seconds from game start
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    team: Mapped[int | None] = mapped_column(Integer)
    key: Mapped[str | None] = mapped_column(String(64))
