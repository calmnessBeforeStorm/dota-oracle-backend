"""Login, rotation and theft detection (design 2026-09-11-auth-and-pipeline-panel, section 1).

Each call gets its own session, the way each request does: one shared session would answer
from its identity map and hide what a concurrent request actually sees.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth import service
from app.auth.lockout import LOCK_SECONDS, failure_key, lock_key
from app.auth.service import (
    InvalidCredentialsError,
    IssuedTokens,
    LoginLockedError,
    RefreshRejectedError,
)
from app.auth.tokens import decode_access_token, decode_refresh_token, token_hash
from app.db.models.auth import AuthSession, User
from tests.auth.conftest import PASSWORD, SECRET, FakeRedis, MakeUser

Sessions = async_sessionmaker[AsyncSession]
IP = "10.0.0.1"


class _Recorder:
    """Stands in for the module logger. Not structlog's capture_logs: that one misses loggers
    cached before it was entered, and any test that runs the app lifespan first turns caching on."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def info(self, event: str, **fields: Any) -> None:
        self.events.append({"event": event, **fields})

    warning = info


@pytest.fixture
def logs(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    recorder = _Recorder()
    monkeypatch.setattr(service, "log", recorder)
    return recorder.events


async def _login(sessions: Sessions, redis: FakeRedis, password: str = PASSWORD) -> IssuedTokens:
    async with sessions() as session:
        return await service.login(
            session, redis, username="adilet", password=password, ip=IP, secret=SECRET
        )


async def _refresh(sessions: Sessions, token: str) -> IssuedTokens:
    async with sessions() as session:
        return await service.refresh(session, refresh_token=token, ip=IP, secret=SECRET)


async def _rows(sessions: Sessions) -> list[AuthSession]:
    async with sessions() as session:
        return list(await session.scalars(select(AuthSession)))


def _spy_on_verify(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    checked: list[str] = []
    real: Callable[[str, str], bool] = service.verify_password

    def spy(password_hash: str, password: str) -> bool:
        checked.append(password_hash)
        return real(password_hash, password)

    monkeypatch.setattr(service, "verify_password", spy)
    return checked


class TestLogin:
    async def test_issues_tokens_and_records_the_session(
        self, sessionmaker: Sessions, fake_redis: FakeRedis, make_user: MakeUser
    ) -> None:
        user = await make_user()
        issued = await _login(sessionmaker, fake_redis)

        access = decode_access_token(issued.access_token, SECRET)
        assert access.user_id == user.id
        assert access.permissions == tuple(user.permissions)

        claims = decode_refresh_token(issued.refresh_token, SECRET)
        (row,) = await _rows(sessionmaker)
        assert row.id == claims.session_id
        assert row.user_id == user.id
        assert row.token_hash == token_hash(issued.refresh_token)
        assert row.revoked_at is None
        assert row.expires_at > datetime.now(UTC) + timedelta(days=6)

    async def test_a_wrong_password_is_refused_and_counted(
        self, sessionmaker: Sessions, fake_redis: FakeRedis, make_user: MakeUser
    ) -> None:
        await make_user()
        with pytest.raises(InvalidCredentialsError):
            await _login(sessionmaker, fake_redis, password="wrong")
        assert fake_redis.values[failure_key("login", "adilet")] == "1"
        assert await _rows(sessionmaker) == []

    async def test_an_unknown_login_still_spends_a_hash_check(
        self, sessionmaker: Sessions, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        checked = _spy_on_verify(monkeypatch)
        with pytest.raises(InvalidCredentialsError):
            await _login(sessionmaker, fake_redis)
        assert checked == [service.dummy_hash()]

    async def test_an_inactive_user_cannot_log_in(
        self, sessionmaker: Sessions, fake_redis: FakeRedis, make_user: MakeUser
    ) -> None:
        await make_user(is_active=False)
        with pytest.raises(InvalidCredentialsError):
            await _login(sessionmaker, fake_redis)

    async def test_a_locked_login_is_refused_without_checking_the_password(
        self,
        sessionmaker: Sessions,
        fake_redis: FakeRedis,
        make_user: MakeUser,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await make_user()
        await fake_redis.set(lock_key("login", "adilet"), "1", ex=LOCK_SECONDS)
        checked = _spy_on_verify(monkeypatch)
        with pytest.raises(LoginLockedError) as caught:
            await _login(sessionmaker, fake_redis)
        assert caught.value.retry_after == LOCK_SECONDS
        assert checked == []

    async def test_success_resets_the_login_counter(
        self, sessionmaker: Sessions, fake_redis: FakeRedis, make_user: MakeUser
    ) -> None:
        await make_user()
        with pytest.raises(InvalidCredentialsError):
            await _login(sessionmaker, fake_redis, password="wrong")
        await _login(sessionmaker, fake_redis)
        assert failure_key("login", "adilet") not in fake_redis.values

    async def test_the_password_is_never_logged(
        self,
        sessionmaker: Sessions,
        fake_redis: FakeRedis,
        make_user: MakeUser,
        logs: list[dict[str, Any]],
    ) -> None:
        await make_user()
        issued = await _login(sessionmaker, fake_redis)
        with pytest.raises(InvalidCredentialsError):
            await _login(sessionmaker, fake_redis, password="wrong-password-value")
        rendered = repr(logs)
        assert PASSWORD not in rendered
        assert "wrong-password-value" not in rendered
        assert issued.refresh_token not in rendered
        assert issued.access_token not in rendered
        assert {"auth.login_succeeded", "auth.login_failed"} <= {e["event"] for e in logs}


class TestRefresh:
    async def test_rotates_the_token(
        self, sessionmaker: Sessions, fake_redis: FakeRedis, make_user: MakeUser
    ) -> None:
        await make_user()
        first = await _login(sessionmaker, fake_redis)
        second = await _refresh(sessionmaker, first.refresh_token)

        assert second.refresh_token != first.refresh_token
        (row,) = await _rows(sessionmaker)
        assert row.token_hash == token_hash(second.refresh_token)
        assert decode_refresh_token(second.refresh_token, SECRET).session_id == row.id

    async def test_reusing_a_rotated_token_revokes_every_session_of_the_user(
        self,
        sessionmaker: Sessions,
        fake_redis: FakeRedis,
        make_user: MakeUser,
        logs: list[dict[str, Any]],
    ) -> None:
        await make_user()
        laptop = await _login(sessionmaker, fake_redis)
        await _login(sessionmaker, fake_redis)  # a second device
        await _refresh(sessionmaker, laptop.refresh_token)

        with pytest.raises(RefreshRejectedError):
            await _refresh(sessionmaker, laptop.refresh_token)

        rows = await _rows(sessionmaker)
        assert len(rows) == 2
        assert all(row.revoked_at is not None for row in rows)
        (event,) = [e for e in logs if e["event"] == "auth.refresh_reuse"]
        assert event["ip"] == IP
        assert laptop.refresh_token not in repr(logs)

    async def test_a_revoked_session_cannot_refresh(
        self, sessionmaker: Sessions, fake_redis: FakeRedis, make_user: MakeUser
    ) -> None:
        await make_user()
        issued = await _login(sessionmaker, fake_redis)
        async with sessionmaker() as session:
            await session.execute(update(AuthSession).values(revoked_at=datetime.now(UTC)))
            await session.commit()
        with pytest.raises(RefreshRejectedError):
            await _refresh(sessionmaker, issued.refresh_token)

    async def test_an_expired_session_cannot_refresh(
        self, sessionmaker: Sessions, fake_redis: FakeRedis, make_user: MakeUser
    ) -> None:
        await make_user()
        issued = await _login(sessionmaker, fake_redis)
        async with sessionmaker() as session:
            await session.execute(
                update(AuthSession).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
            await session.commit()
        with pytest.raises(RefreshRejectedError):
            await _refresh(sessionmaker, issued.refresh_token)

    async def test_a_deactivated_user_cannot_refresh(
        self, sessionmaker: Sessions, fake_redis: FakeRedis, make_user: MakeUser
    ) -> None:
        user = await make_user()
        issued = await _login(sessionmaker, fake_redis)
        async with sessionmaker() as session:
            stored = await session.get(User, user.id)
            assert stored is not None
            stored.is_active = False
            await session.commit()
        with pytest.raises(RefreshRejectedError):
            await _refresh(sessionmaker, issued.refresh_token)

    async def test_an_access_token_cannot_refresh(
        self, sessionmaker: Sessions, fake_redis: FakeRedis, make_user: MakeUser
    ) -> None:
        await make_user()
        issued = await _login(sessionmaker, fake_redis)
        with pytest.raises(RefreshRejectedError):
            await _refresh(sessionmaker, issued.access_token)


class TestLogout:
    async def test_revokes_the_session(
        self, sessionmaker: Sessions, fake_redis: FakeRedis, make_user: MakeUser
    ) -> None:
        await make_user()
        issued = await _login(sessionmaker, fake_redis)
        async with sessionmaker() as session:
            await service.logout(session, refresh_token=issued.refresh_token, secret=SECRET)
        (row,) = await _rows(sessionmaker)
        assert row.revoked_at is not None
        with pytest.raises(RefreshRejectedError):
            await _refresh(sessionmaker, issued.refresh_token)

    @pytest.mark.parametrize("token", [None, "", "garbage"])
    async def test_without_a_usable_token_is_a_no_op(
        self, sessionmaker: Sessions, token: str | None
    ) -> None:
        async with sessionmaker() as session:
            await service.logout(session, refresh_token=token, secret=SECRET)
