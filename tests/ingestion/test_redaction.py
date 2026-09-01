"""Credentials must never survive into a log line or an exception message.

httpx puts the full request URL into HTTPStatusError, and every external source here takes
its key as a query parameter, so an upstream 400 would otherwise ship the Steam key to
structlog and Sentry.

It also logs the URL on its own account, at INFO, on every single request - which scrubbing
our own messages does nothing about. That one shipped the Steam key into
`docker compose logs worker` twice a minute for as long as the poller has existed, so the
filter that closes it is tested here alongside the function it reuses.
"""

import logging

import httpx
import pytest

from app.core.logging import RedactingFilter, configure_logging
from app.core.redaction import redact
from app.ingestion.clients.base import raise_for_status

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


def _record(msg: str, *args: object) -> logging.LogRecord:
    return logging.LogRecord("httpx", logging.INFO, __file__, 1, msg, args or None, None)


def test_filter_scrubs_a_message_written_by_someone_else() -> None:
    """The httpx line verbatim: nothing of ours builds it, so nothing of ours redacted it."""
    record = _record(
        f'HTTP Request: GET https://api.steampowered.com/v1/?key={STEAM_KEY} "HTTP/1.1 200 OK"'
    )
    RedactingFilter().filter(record)

    assert STEAM_KEY not in record.getMessage()
    assert "key=***" in record.getMessage()
    # Still has to say what happened, or the redaction has cost us the log line.
    assert "200 OK" in record.getMessage()


def test_filter_scrubs_lazy_formatting_arguments() -> None:
    """A record whose secret is in `args` is not a message yet, and is the common shape."""
    record = _record("HTTP Request: %s", f"GET https://x/?api_key={STEAM_KEY}")
    RedactingFilter().filter(record)

    assert STEAM_KEY not in record.getMessage()
    assert "api_key=***" in record.getMessage()


def test_filter_leaves_records_without_credentials_alone() -> None:
    record = _record("live_poll.tick games=%d", 11)
    RedactingFilter().filter(record)
    assert record.getMessage() == "live_poll.tick games=11"


def test_configure_logging_installs_the_filter_once() -> None:
    """Called on every worker and API start-up; it must not stack filters per call."""
    configure_logging("INFO")
    configure_logging("INFO")

    for handler in logging.getLogger().handlers:
        installed = [f for f in handler.filters if isinstance(f, RedactingFilter)]
        assert len(installed) <= 1
    assert any(
        isinstance(f, RedactingFilter)
        for handler in logging.getLogger().handlers
        for f in handler.filters
    )
