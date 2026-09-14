"""The /api/auth endpoints over HTTP: status codes, the cookie, the headers."""

import httpx
import pytest

from app.api.routes import auth as auth_routes
from app.auth.lockout import LOCK_SECONDS, lock_key
from app.core.config import Settings
from tests.auth.conftest import PASSWORD, FakeRedis, MakeUser

LOGIN = "/api/auth/login"


def _set_cookie(response: httpx.Response) -> str:
    (header,) = [
        h for h in response.headers.get_list("set-cookie") if h.startswith("refresh_token=")
    ]
    return header


def _cookie_value(response: httpx.Response) -> str:
    return _set_cookie(response).split(";", 1)[0].removeprefix("refresh_token=")


async def _post_with_cookie(api: httpx.AsyncClient, path: str, token: str) -> httpx.Response:
    # Explicit, so a test decides exactly which token is presented; the client's jar stays out.
    api.cookies.clear()
    return await api.post(path, headers={"Cookie": f"refresh_token={token}"})


async def _login(api: httpx.AsyncClient) -> httpx.Response:
    response = await api.post(LOGIN, json={"username": "adilet", "password": PASSWORD})
    assert response.status_code == 200, response.text
    return response


async def test_login_answers_with_the_access_token_and_the_user(
    api: httpx.AsyncClient, make_user: MakeUser
) -> None:
    await make_user(permissions=["pipeline.runs.view"])
    body = (await _login(api)).json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 900
    assert body["access_token"]
    assert body["user"]["username"] == "adilet"
    assert body["user"]["display_name"] == "Adilet"
    assert body["user"]["permissions"] == ["pipeline.runs.view"]
    assert "refresh_token" not in body


async def test_the_refresh_cookie_is_locked_down(
    api: httpx.AsyncClient, make_user: MakeUser
) -> None:
    await make_user()
    header = _set_cookie(await _login(api))
    assert "HttpOnly" in header
    assert "samesite=strict" in header.lower()
    assert "Path=/api/auth" in header
    assert "Max-Age=604800" in header
    # Local: a browser would drop a Secure cookie set over plain http.
    assert "Secure" not in header


async def test_the_cookie_is_secure_in_production(
    api: httpx.AsyncClient, make_user: MakeUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    production = Settings(_env_file=None, app_env="production", secret_key="p" * 48)
    monkeypatch.setattr(auth_routes, "get_settings", lambda: production)
    await make_user()
    assert "Secure" in _set_cookie(await _login(api))


async def test_an_unknown_login_and_a_wrong_password_look_the_same(
    api: httpx.AsyncClient, make_user: MakeUser
) -> None:
    await make_user()
    wrong = await api.post(LOGIN, json={"username": "adilet", "password": "wrong"})
    unknown = await api.post(LOGIN, json={"username": "nobody", "password": PASSWORD})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json() == {"detail": auth_routes.INVALID_CREDENTIALS}


async def test_a_locked_login_is_429_with_retry_after(
    api: httpx.AsyncClient, make_user: MakeUser, fake_redis: FakeRedis
) -> None:
    await make_user()
    await fake_redis.set(lock_key("login", "adilet"), "1", ex=LOCK_SECONDS)
    response = await api.post(LOGIN, json={"username": "adilet", "password": PASSWORD})
    assert response.status_code == 429
    assert response.headers["retry-after"] == str(LOCK_SECONDS)


async def test_login_validates_the_body(api: httpx.AsyncClient) -> None:
    assert (await api.post(LOGIN, json={"username": "adilet"})).status_code == 422


async def test_refresh_returns_a_new_pair_and_a_new_cookie(
    api: httpx.AsyncClient, make_user: MakeUser
) -> None:
    await make_user()
    first = await _login(api)
    second = await _post_with_cookie(api, "/api/auth/refresh", _cookie_value(first))
    assert second.status_code == 200
    assert second.json()["access_token"]
    assert _cookie_value(second) != _cookie_value(first)


async def test_refresh_without_a_cookie_is_401(api: httpx.AsyncClient) -> None:
    api.cookies.clear()
    assert (await api.post("/api/auth/refresh")).status_code == 401


async def test_a_rejected_refresh_clears_the_cookie(
    api: httpx.AsyncClient, make_user: MakeUser
) -> None:
    await make_user()
    old = _cookie_value(await _login(api))
    await _post_with_cookie(api, "/api/auth/refresh", old)
    reused = await _post_with_cookie(api, "/api/auth/refresh", old)
    assert reused.status_code == 401
    assert reused.json() == {"detail": auth_routes.SESSION_REJECTED}
    assert "Max-Age=0" in _set_cookie(reused)


async def test_logout_clears_the_cookie_and_ends_the_session(
    api: httpx.AsyncClient, make_user: MakeUser
) -> None:
    await make_user()
    token = _cookie_value(await _login(api))
    response = await _post_with_cookie(api, "/api/auth/logout", token)
    assert response.status_code == 204
    assert "Max-Age=0" in _set_cookie(response)
    assert (await _post_with_cookie(api, "/api/auth/refresh", token)).status_code == 401


async def test_logout_without_a_cookie_is_still_204(api: httpx.AsyncClient) -> None:
    api.cookies.clear()
    assert (await api.post("/api/auth/logout")).status_code == 204


async def test_me_reads_the_bearer_token(api: httpx.AsyncClient, make_user: MakeUser) -> None:
    user = await make_user()
    access = (await _login(api)).json()["access_token"]
    response = await api.get("/api/auth/me", headers={"Authorization": f"Bearer {access}"})
    assert response.status_code == 200
    assert response.json() == {
        "id": str(user.id),
        "username": "adilet",
        "display_name": "Adilet",
        "permissions": list(user.permissions),
    }


async def test_me_without_a_token_is_401(api: httpx.AsyncClient) -> None:
    assert (await api.get("/api/auth/me")).status_code == 401
