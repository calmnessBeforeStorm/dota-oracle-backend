"""Signed access and refresh tokens.

Signed, not encrypted: anybody holding a token can read its claims, so nothing secret goes in.
"""

import hashlib
import secrets
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import jwt

ALGORITHM = "HS256"
ACCESS_TTL = timedelta(minutes=15)
REFRESH_TTL = timedelta(days=7)


class InvalidTokenError(Exception):
    """Bad signature, expired, malformed, or a token of the other type."""


@dataclass(frozen=True)
class AccessClaims:
    user_id: uuid.UUID
    username: str
    display_name: str
    permissions: tuple[str, ...]


@dataclass(frozen=True)
class RefreshClaims:
    user_id: uuid.UUID
    session_id: uuid.UUID


def issue_access_token(
    *,
    user_id: uuid.UUID,
    username: str,
    display_name: str,
    permissions: Sequence[str],
    secret: str,
    now: datetime,
) -> str:
    payload = {
        "sub": str(user_id),
        "username": username,
        "display_name": display_name,
        "permissions": list(permissions),
        "token_type": "access",
        "exp": now + ACCESS_TTL,
    }
    return jwt.encode(payload, secret, algorithm=ALGORITHM)


def issue_refresh_token(
    *, user_id: uuid.UUID, session_id: uuid.UUID, secret: str, now: datetime
) -> str:
    payload = {
        "sub": str(user_id),
        "sid": str(session_id),
        # Two tokens issued for one session in the same second would otherwise be identical,
        # and rotation would store the hash the client already presented.
        "jti": secrets.token_urlsafe(16),
        "token_type": "refresh",
        "exp": now + REFRESH_TTL,
    }
    return jwt.encode(payload, secret, algorithm=ALGORITHM)


def _decode(token: str, secret: str, token_type: str) -> dict[str, Any]:
    try:
        claims: dict[str, Any] = jwt.decode(
            token, secret, algorithms=[ALGORITHM], options={"require": ["exp", "sub"]}
        )
    except jwt.PyJWTError as exc:
        raise InvalidTokenError(type(exc).__name__) from exc
    # Without this a refresh token, which lives a week, would open every endpoint an access
    # token opens.
    if claims.get("token_type") != token_type:
        raise InvalidTokenError(f"expected a {token_type} token")
    return claims


def _uuid(value: object) -> uuid.UUID:
    if not isinstance(value, str):
        raise InvalidTokenError("malformed id")
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise InvalidTokenError("malformed id") from exc


def decode_access_token(token: str, secret: str) -> AccessClaims:
    claims = _decode(token, secret, "access")
    username = claims.get("username")
    display_name = claims.get("display_name")
    permissions = claims.get("permissions")
    if not isinstance(username, str) or not isinstance(display_name, str):
        raise InvalidTokenError("malformed user")
    if not isinstance(permissions, list) or not all(isinstance(p, str) for p in permissions):
        raise InvalidTokenError("malformed permissions")
    return AccessClaims(
        user_id=_uuid(claims["sub"]),
        username=username,
        display_name=display_name,
        permissions=tuple(permissions),
    )


def decode_refresh_token(token: str, secret: str) -> RefreshClaims:
    claims = _decode(token, secret, "refresh")
    return RefreshClaims(user_id=_uuid(claims["sub"]), session_id=_uuid(claims.get("sid")))


def token_hash(token: str) -> str:
    """What the database keeps instead of the token: a leaked row cannot be replayed."""
    return hashlib.sha256(token.encode()).hexdigest()
