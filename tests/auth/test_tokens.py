"""Access and refresh tokens (design 2026-09-11-auth-and-pipeline-panel, section 1)."""

import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.auth.tokens import (
    ACCESS_TTL,
    REFRESH_TTL,
    InvalidTokenError,
    decode_access_token,
    decode_refresh_token,
    issue_access_token,
    issue_refresh_token,
    token_hash,
)

SECRET = "test-secret-key-long-enough-for-hs256-signing"
USER_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
SESSION_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")


def _access(now: datetime | None = None, secret: str = SECRET) -> str:
    return issue_access_token(
        user_id=USER_ID,
        username="adilet",
        display_name="Adilet",
        permissions=["pipeline.runs.view"],
        secret=secret,
        now=now or datetime.now(UTC),
    )


def _refresh(now: datetime | None = None) -> str:
    return issue_refresh_token(
        user_id=USER_ID, session_id=SESSION_ID, secret=SECRET, now=now or datetime.now(UTC)
    )


def test_an_access_token_carries_the_user() -> None:
    claims = decode_access_token(_access(), SECRET)
    assert claims.user_id == USER_ID
    assert claims.username == "adilet"
    assert claims.display_name == "Adilet"
    assert claims.permissions == ("pipeline.runs.view",)


def test_a_refresh_token_carries_the_session() -> None:
    claims = decode_refresh_token(_refresh(), SECRET)
    assert claims.user_id == USER_ID
    assert claims.session_id == SESSION_ID


def test_lifetimes_follow_the_spec() -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    access = jwt.decode(_access(now), SECRET, algorithms=["HS256"])
    refresh = jwt.decode(_refresh(now), SECRET, algorithms=["HS256"])
    assert access["exp"] == int((now + ACCESS_TTL).timestamp())
    assert refresh["exp"] == int((now + REFRESH_TTL).timestamp())
    assert timedelta(minutes=15) == ACCESS_TTL
    assert timedelta(days=7) == REFRESH_TTL


def test_two_refresh_tokens_of_one_session_never_match() -> None:
    now = datetime.now(UTC)
    assert _refresh(now) != _refresh(now)


def test_an_expired_token_is_refused() -> None:
    long_ago = datetime.now(UTC) - REFRESH_TTL - timedelta(seconds=5)
    with pytest.raises(InvalidTokenError):
        decode_access_token(_access(long_ago), SECRET)
    with pytest.raises(InvalidTokenError):
        decode_refresh_token(_refresh(long_ago), SECRET)


def test_a_token_signed_with_another_key_is_refused() -> None:
    with pytest.raises(InvalidTokenError):
        decode_access_token(_access(secret="another-secret-key-long-enough-for-hs256"), SECRET)


def test_an_access_token_is_not_a_refresh_token() -> None:
    with pytest.raises(InvalidTokenError):
        decode_refresh_token(_access(), SECRET)


def test_a_refresh_token_is_not_an_access_token() -> None:
    with pytest.raises(InvalidTokenError):
        decode_access_token(_refresh(), SECRET)


def test_garbage_is_refused() -> None:
    with pytest.raises(InvalidTokenError):
        decode_access_token("not.a.token", SECRET)


def test_a_malformed_subject_is_refused() -> None:
    token = jwt.encode(
        {
            "sub": "not-a-uuid",
            "sid": str(SESSION_ID),
            "token_type": "refresh",
            "exp": datetime.now(UTC) + REFRESH_TTL,
        },
        SECRET,
        algorithm="HS256",
    )
    with pytest.raises(InvalidTokenError):
        decode_refresh_token(token, SECRET)


def test_the_stored_hash_is_sha256_hex() -> None:
    digest = token_hash("abc")
    assert digest == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
