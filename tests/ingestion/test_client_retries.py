"""Which HTTP failures are worth a second try.

Every status error used to be retried four times with exponential backoff. For a 5xx that is
right: the server is struggling and may recover. For a 4xx it is only cost. Two measurements
from the production server on 2026-09-11:

- STRATZ refused the host with 403 on every request, so each match cost four refused
  requests instead of one - eighty an hour against a block that was never going to lift.
- OpenDota answers 404 for a match that has not been published yet, which is every match
  still being played. Each cost four requests and about fifteen seconds, against a daily
  allowance that the server's own predictions already come close to.

A 404 is also not a failure for the caller. It means "not yet", and counting it as one fed
the "give up after twenty in a row" rule: the resolver works newest first, the newest matches
are the ones still running, and at a busy hour it would have given up before reaching a
single finished match.
"""

from typing import Any

import httpx
import pytest

from app.ingestion.clients.base import RateLimitedError, is_transient
from app.ingestion.clients.opendota import OpenDotaClient


def status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://example.test/x")
    return httpx.HTTPStatusError(
        "boom", request=request, response=httpx.Response(code, request=request)
    )


class TestWhatIsRetried:
    @pytest.mark.parametrize("code", [500, 502, 503, 504, 408])
    def test_server_trouble_is_retried(self, code: int) -> None:
        assert is_transient(status_error(code))

    @pytest.mark.parametrize("code", [400, 401, 403, 404, 422])
    def test_a_client_error_is_not(self, code: int) -> None:
        # Asking again gets the same answer, only later and at the same cost.
        assert not is_transient(status_error(code))

    def test_transport_failures_and_rate_limits_still_are(self) -> None:
        assert is_transient(httpx.ConnectError("reset"))
        assert is_transient(RateLimitedError(retry_after=30))


class TestOpenDotaNotFound:
    async def test_an_unpublished_match_is_an_empty_payload_asked_once(self) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            return httpx.Response(404, json={"error": "Not Found"})

        client = OpenDotaClient()
        client.min_interval = 0
        await client._client.aclose()
        client._client = httpx.AsyncClient(
            base_url=client.base_url, transport=httpx.MockTransport(handler)
        )
        async with client:
            payload: dict[str, Any] = await client.match(8992789668)

        assert payload == {}
        assert calls == ["/api/matches/8992789668"]

    async def test_other_errors_still_raise(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, text="<!DOCTYPE html>")

        client = OpenDotaClient()
        client.min_interval = 0
        await client._client.aclose()
        client._client = httpx.AsyncClient(
            base_url=client.base_url, transport=httpx.MockTransport(handler)
        )
        async with client:
            with pytest.raises(httpx.HTTPStatusError):
                await client.match(1)
