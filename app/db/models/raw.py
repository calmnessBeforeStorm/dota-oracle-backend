"""Raw layer (spec section 4.1). Payloads are stored whole and forever: features get
re-derived dozens of times and re-downloading is not an option (quotas, time, sources vanish)."""

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class RawMatch(Base):
    __tablename__ = "raw_matches"
    __table_args__ = (UniqueConstraint("match_id", "source", name="uq_raw_matches_match_source"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    match_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)  # opendota | stratz | steam
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class RawLiveSnapshot(Base):
    """Raw GetRealtimeStats responses, one row per poll."""

    __tablename__ = "raw_live_snapshots"
    __table_args__ = (Index("ix_raw_live_snapshots_match_captured", "match_id", "captured_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    match_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    server_steam_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class RawLiquipedia(Base):
    __tablename__ = "raw_liquipedia"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class IngestCheckpoint(Base):
    """Backfill progress (spec section 4.4) so a restarted worker resumes instead of redoing."""

    __tablename__ = "ingest_checkpoints"

    source: Mapped[str] = mapped_column(String(64), primary_key=True)
    cursor: Mapped[str | None] = mapped_column(String(128), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
