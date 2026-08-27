"""Writes into the raw layer.

Every write here is an upsert on a natural key, and progress lives in `ingest_checkpoints`
(spec section 4.4). Restarting a worker must never produce duplicates - that is a stated
acceptance criterion of phase 1, not a nicety: the backfill runs for hours and will be
interrupted.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.raw import IngestCheckpoint, RawMatch
from app.ingestion.sources import Checkpoint, RawSource


def utcnow() -> datetime:
    """All timestamps are UTC (spec section 4.4)."""
    return datetime.now(UTC)


async def upsert_raw_matches(
    session: AsyncSession,
    source: RawSource,
    payloads: Sequence[dict[str, Any]],
    fetched_at: datetime | None = None,
) -> int:
    """Store raw payloads, replacing any earlier copy of the same (match_id, source).

    Returns the number of rows written. Re-running with the same input refreshes the rows
    in place and leaves the row count unchanged - that is what makes the backfill resumable.
    """
    rows = [
        {
            "match_id": int(payload["match_id"]),
            "source": str(source),
            "fetched_at": fetched_at or utcnow(),
            "payload": payload,
        }
        for payload in payloads
        if payload.get("match_id") is not None
    ]
    if not rows:
        return 0

    statement = insert(RawMatch).values(rows)
    statement = statement.on_conflict_do_update(
        index_elements=[RawMatch.match_id, RawMatch.source],
        set_={
            "fetched_at": statement.excluded.fetched_at,
            "payload": statement.excluded.payload,
        },
    )
    await session.execute(statement)
    return len(rows)


async def get_checkpoint(session: AsyncSession, key: Checkpoint) -> str | None:
    result = await session.execute(
        select(IngestCheckpoint.cursor).where(IngestCheckpoint.source == str(key))
    )
    return result.scalar_one_or_none()


async def set_checkpoint(session: AsyncSession, key: Checkpoint, cursor: str | None) -> None:
    statement = insert(IngestCheckpoint).values(source=str(key), cursor=cursor, updated_at=utcnow())
    statement = statement.on_conflict_do_update(
        index_elements=[IngestCheckpoint.source],
        set_={"cursor": statement.excluded.cursor, "updated_at": statement.excluded.updated_at},
    )
    await session.execute(statement)


async def count_raw_matches(session: AsyncSession, source: RawSource | None = None) -> int:
    """Used by the CLI and by the idempotency tests."""
    statement = select(RawMatch.id)
    if source is not None:
        statement = statement.where(RawMatch.source == str(source))
    result = await session.execute(statement)
    return len(result.scalars().all())
