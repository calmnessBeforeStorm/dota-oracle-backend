"""Login, refresh and logout (design 2026-09-11-auth-and-pipeline-panel, section 1).

Everything that touches the database lives here; the routes only translate these exceptions into
status codes and set or clear the cookie. Nothing in this module logs a token or a password.
"""

import asyncio
import hmac
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import lockout
from app.auth.passwords import dummy_hash, verify_password
from app.auth.tokens import (
    REFRESH_TTL,
    InvalidTokenError,
    decode_refresh_token,
    issue_access_token,
    issue_refresh_token,
    token_hash,
)
from app.core.logging import get_logger
from app.db.models.auth import AuthSession, User

log = get_logger(__name__)


class InvalidCredentialsError(Exception):
    """Unknown login, wrong password or inactive user - deliberately indistinguishable."""


class LoginLockedError(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__(f"locked for {retry_after}s")
        self.retry_after = retry_after


class RefreshRejectedError(Exception):
    """The refresh token does not open a live session. The cookie should be cleared."""


@dataclass(frozen=True)
class IssuedTokens:
    access_token: str
    refresh_token: str
    user: User


def _now() -> datetime:
    return datetime.now(UTC)


def _access_token(user: User, secret: str, now: datetime) -> str:
    return issue_access_token(
        user_id=user.id,
        username=user.username,
        display_name=user.display_name,
        permissions=user.permissions,
        secret=secret,
        now=now,
    )


async def login(
    session: AsyncSession,
    redis: Redis,
    *,
    username: str,
    password: str,
    ip: str,
    secret: str,
) -> IssuedTokens:
    wait = await lockout.retry_after(redis, username=username, ip=ip)
    if wait is not None:
        log.info("auth.login_locked", username=username, ip=ip, retry_after=wait)
        raise LoginLockedError(wait)

    user = await session.scalar(select(User).where(User.username == username))
    # Argon2 is CPU work measured in tens of milliseconds; off the event loop, or every other
    # request waits for it.
    stored = user.password_hash if user is not None else dummy_hash()
    matches = await asyncio.to_thread(verify_password, stored, password)
    if user is None or not user.is_active or not matches:
        await lockout.record_failure(redis, username=username, ip=ip)
        log.info("auth.login_failed", username=username, ip=ip)
        raise InvalidCredentialsError

    await lockout.reset_login(redis, username=username)
    now = _now()
    row = AuthSession(
        id=uuid.uuid4(),
        user_id=user.id,
        token_hash="",
        created_at=now,
        last_used_at=now,
        expires_at=now + REFRESH_TTL,
    )
    refresh_token = issue_refresh_token(user_id=user.id, session_id=row.id, secret=secret, now=now)
    row.token_hash = token_hash(refresh_token)
    session.add(row)
    await session.commit()
    log.info("auth.login_succeeded", username=username, ip=ip, session_id=str(row.id))
    return IssuedTokens(_access_token(user, secret, now), refresh_token, user)


async def revoke_all_sessions(session: AsyncSession, user_id: uuid.UUID) -> int:
    """Revoke every live session of a user. The caller commits."""
    result = await session.execute(
        update(AuthSession)
        .where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=_now())
    )
    # CursorResult carries rowcount; the base Result type mypy infers here does not.
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


async def _reject_reuse(session: AsyncSession, user_id: uuid.UUID, ip: str) -> None:
    revoked = await revoke_all_sessions(session, user_id)
    await session.commit()
    log.warning("auth.refresh_reuse", user_id=str(user_id), ip=ip, revoked_sessions=revoked)


async def refresh(
    session: AsyncSession, *, refresh_token: str, ip: str, secret: str
) -> IssuedTokens:
    try:
        claims = decode_refresh_token(refresh_token, secret)
    except InvalidTokenError:
        raise RefreshRejectedError from None

    now = _now()
    row = await session.get(AuthSession, claims.session_id, populate_existing=True)
    if (
        row is None
        or row.user_id != claims.user_id
        or row.revoked_at is not None
        or row.expires_at <= now
    ):
        raise RefreshRejectedError

    presented = token_hash(refresh_token)
    if not hmac.compare_digest(row.token_hash, presented):
        await _reject_reuse(session, row.user_id, ip)
        raise RefreshRejectedError

    user = await session.get(User, row.user_id, populate_existing=True)
    if user is None or not user.is_active:
        raise RefreshRejectedError

    new_token = issue_refresh_token(user_id=user.id, session_id=row.id, secret=secret, now=now)
    # Conditional on the hash just checked: two requests presenting the same token both pass the
    # comparison above, and without this condition both would rotate. The loser is presenting a
    # token that is no longer current, which is the reuse case by definition.
    rotated = await session.execute(
        update(AuthSession)
        .where(
            AuthSession.id == row.id,
            AuthSession.token_hash == presented,
            AuthSession.revoked_at.is_(None),
        )
        .values(token_hash=token_hash(new_token), last_used_at=now)
    )
    # CursorResult carries rowcount; the base Result type mypy infers here does not.
    if int(rotated.rowcount or 0) != 1:  # type: ignore[attr-defined]
        await session.rollback()
        # row is expired after rollback; claims.user_id was already checked equal to it above,
        # and unlike an ORM attribute it needs no I/O to read.
        await _reject_reuse(session, claims.user_id, ip)
        raise RefreshRejectedError

    await session.commit()
    return IssuedTokens(_access_token(user, secret, now), new_token, user)


async def logout(session: AsyncSession, *, refresh_token: str | None, secret: str) -> None:
    """Revoke the session the token belongs to. Idempotent: no token, a bad one or an already
    revoked session all end the same way, because failing a logout protects nothing."""
    if not refresh_token:
        return
    try:
        claims = decode_refresh_token(refresh_token, secret)
    except InvalidTokenError:
        return
    await session.execute(
        update(AuthSession)
        .where(AuthSession.id == claims.session_id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=_now())
    )
    await session.commit()
