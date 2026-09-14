"""Permission checks from the access token, without the database."""

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.auth.dependencies import require_permission
from app.auth.tokens import ACCESS_TTL, AccessClaims, issue_access_token, issue_refresh_token
from app.core.config import get_settings

CODE = "pipeline.run.status"


def _client() -> TestClient:
    app = FastAPI()

    @app.get("/guarded")
    async def guarded(claims: AccessClaims = Depends(require_permission(CODE))) -> dict[str, str]:
        return {"user": claims.username}

    return TestClient(app)


def _bearer(permissions: list[str], now: datetime | None = None) -> dict[str, str]:
    token = issue_access_token(
        user_id=uuid.uuid4(),
        username="adilet",
        display_name="Adilet",
        permissions=permissions,
        secret=get_settings().secret_key,
        now=now or datetime.now(UTC),
    )
    return {"Authorization": f"Bearer {token}"}


def test_no_token_is_401() -> None:
    response = _client().get("/guarded")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_a_garbage_token_is_401() -> None:
    assert _client().get("/guarded", headers={"Authorization": "Bearer x.y.z"}).status_code == 401


def test_another_scheme_is_401() -> None:
    assert _client().get("/guarded", headers={"Authorization": "Basic abc"}).status_code == 401


def test_an_expired_token_is_401() -> None:
    stale = datetime.now(UTC) - ACCESS_TTL - timedelta(seconds=5)
    assert _client().get("/guarded", headers=_bearer([CODE], stale)).status_code == 401


def test_a_refresh_token_is_not_a_bearer_token() -> None:
    token = issue_refresh_token(
        user_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        secret=get_settings().secret_key,
        now=datetime.now(UTC),
    )
    response = _client().get("/guarded", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


def test_a_missing_permission_is_403() -> None:
    response = _client().get("/guarded", headers=_bearer(["pipeline.runs.view"]))
    assert response.status_code == 403


def test_a_granted_permission_passes() -> None:
    response = _client().get("/guarded", headers=_bearer([CODE]))
    assert response.status_code == 200
    assert response.json() == {"user": "adilet"}
