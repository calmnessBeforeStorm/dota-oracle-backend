"""Credentials must never survive into a log line or an exception message.

httpx puts the full request URL into HTTPStatusError, and every external source here takes
its key as a query parameter, so an upstream 400 would otherwise ship the Steam key to
structlog and Sentry.
"""

import httpx
import pytest

from app.ingestion.clients.base import raise_for_status, redact

STEAM_KEY = "D0495A1B2C3D4E5F60718293A4B5C6D7"


def test_redacts_steam_style_key() -> None:
    url = f"https://api.steampowered.com/IDOTA2Match_570/GetLiveLeagueGames/v1/?key={STEAM_KEY}"
    result = redact(url)
    assert STEAM_KEY not in result
    assert "key=***" in result


def test_redacts_every_known_credential_param() -> None:
    url = "https://x/y?api_key=aaa&token=bbb&access_token=ccc&key=ddd"
    result = redact(url)
    assert not any(secret in result for secret in ("aaa", "bbb", "ccc", "ddd"))
    for param in ("api_key", "token", "access_token", "key"):
        assert f"{param}=***" in result


def test_keeps_non_secret_params_readable() -> None:
    """Redaction must not blind the logs: the useful parameters have to survive."""
    result = redact("https://x/y?key=SECRET&match_id=8966639268&less_than_match_id=123")
    assert "SECRET" not in result
    assert "match_id=8966639268" in result
    assert "less_than_match_id=123" in result


def test_api_key_is_not_mangled_into_bare_key() -> None:
    result = redact("https://x/y?api_key=SECRET")
    assert result.endswith("?api_key=***")


def test_raise_for_status_scrubs_the_message() -> None:
    request = httpx.Request("GET", f"https://api.steampowered.com/v1/?key={STEAM_KEY}")
    response = httpx.Response(400, request=request)

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        raise_for_status(response)

    assert STEAM_KEY not in str(exc_info.value)
    assert "key=***" in str(exc_info.value)
    # The status code still has to be readable, otherwise the redaction costs us debugging.
    assert "400" in str(exc_info.value)


def test_raise_for_status_passes_success_through() -> None:
    request = httpx.Request("GET", "https://api.steampowered.com/v1/?key=x")
    raise_for_status(httpx.Response(200, request=request))
