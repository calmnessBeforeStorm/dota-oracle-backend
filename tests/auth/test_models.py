"""The auth tables hold what section 1 says and cascade the way it says."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.auth import AuthSession, User


def _user(username: str = "adilet") -> User:
    return User(
        username=username,
        display_name="Adilet",
        password_hash="$argon2id$placeholder",
        permissions=["pipeline.runs.view"],
    )


async def test_a_user_round_trips(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    async with sessionmaker() as session:
        user = _user()
        session.add(user)
        await session.commit()

    async with sessionmaker() as session:
        stored = await session.scalar(select(User).where(User.username == "adilet"))
    assert stored is not None
    assert isinstance(stored.id, uuid.UUID)
    assert stored.permissions == ["pipeline.runs.view"]
    assert stored.is_active is True
    assert stored.created_at.tzinfo is not None


async def test_usernames_are_unique(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    async with sessionmaker() as session:
        session.add(_user())
        await session.commit()
    async with sessionmaker() as session:
        session.add(_user())
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_deleting_a_user_deletes_their_sessions(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    async with sessionmaker() as session:
        user = _user()
        session.add(user)
        await session.flush()
        session.add(
            AuthSession(user_id=user.id, token_hash="h", expires_at=now + timedelta(days=7))
        )
        await session.commit()
        await session.execute(delete(User).where(User.id == user.id))
        await session.commit()
        assert await session.scalar(select(AuthSession)) is None
