"""The STRATZ client's single-match call (spec section 2.3).

The query text is asserted field by field on purpose. A field dropped from it does not
fail here - it fails much later, inside the adapter, on some matches and not others.
"""

from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from tenacity import stop_after_attempt

from app.ingestion.clients.base import BaseClient, RateLimitedError
from app.ingestion.clients.stratz import MATCH_QUERY, StratzClient


@pytest.fixture
def stratz_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the client a token so it agrees to exist.

    `StratzClient.__init__` refuses to construct without one, which is right - a backfill
    that only discovers the missing token on its first request has already wasted the run.
    But these tests mock the network entirely and CI has no business holding a real
    credential, so one is injected here.

    Patched at the call site rather than through the environment: `get_settings` is
    `lru_cache`d, so setting `STRATZ_API_TOKEN` after import changes nothing.
    """
    monkeypatch.setattr(
        "app.ingestion.clients.stratz.get_settings",
        lambda: SimpleNamespace(stratz_api_token="token-for-tests"),
    )


class FakeQuery:
    """Records what was asked for and returns a canned match."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, query: str, **variables: Any) -> dict[str, Any]:
        self.calls.append((query, variables))
        return {"match": {"id": variables["id"], "durationSeconds": 1800}}


async def test_match_asks_for_the_match_query(
    monkeypatch: pytest.MonkeyPatch, stratz_token: None
) -> None:
    client = StratzClient()
    fake = FakeQuery()
    monkeypatch.setattr(client, "query", fake)

    payload = await client.match(8944612322)

    assert payload["id"] == 8944612322
    query, variables = fake.calls[0]
    assert query is MATCH_QUERY
    assert variables == {"id": 8944612322}
    await client.aclose()


async def test_match_returns_an_empty_mapping_when_stratz_knows_nothing(
    monkeypatch: pytest.MonkeyPatch, stratz_token: None
) -> None:
    """`match(id:)` answers with a null rather than an error for an unknown id."""
    client = StratzClient()

    async def null_match(query: str, **variables: Any) -> dict[str, Any]:
        return {"match": None}

    monkeypatch.setattr(client, "query", null_match)

    assert await client.match(1) == {}
    await client.aclose()


async def test_match_query_asks_for_every_field_the_adapter_needs() -> None:
    for field in (
        "radiantNetworthLeads",
        "radiantExperienceLeads",
        "killEvents",
        "networthPerMinute",
        "towerDeaths",
        "didRadiantWin",
        "durationSeconds",
        "parsedDateTime",
    ):
        assert field in MATCH_QUERY


class TestRateLimiting:
    """A 429 on the POST path must arrive as `RateLimitedError`, not as a status error.

    STRATZ is the only client that posts, and `post_json` used to have neither the 429 check
    nor the retry policy `get_json` has. The consequence was not a crash: its rejections
    looked like ordinary failures, so the detail backfill kept working through its list at
    one refused request every two seconds. Callers decide whether to stop by catching this
    type, so the type is the fix.
    """

    async def test_a_429_becomes_a_rate_limit_error(
        self, monkeypatch: pytest.MonkeyPatch, stratz_token: None
    ) -> None:
        client = StratzClient()

        async def refuse(path: str, **kwargs: Any) -> httpx.Response:
            return httpx.Response(
                429,
                headers={"Retry-After": "30"},
                request=httpx.Request("POST", "https://api.stratz.com/graphql"),
            )

        monkeypatch.setattr(client._client, "post", refuse)
        # One attempt: the retry policy would otherwise back off for minutes before reraising.
        monkeypatch.setattr(
            StratzClient, "post_json", BaseClient.post_json.retry_with(stop=stop_after_attempt(1))
        )  # type: ignore[attr-defined]

        with pytest.raises(RateLimitedError) as caught:
            await client.match(1)

        assert caught.value.retry_after == 30
        await client.aclose()

    async def test_other_failures_stay_status_errors(
        self, monkeypatch: pytest.MonkeyPatch, stratz_token: None
    ) -> None:
        """A 500 is not the server stating a rate, and a caller must not stop a whole
        backfill over one bad response."""
        client = StratzClient()

        async def explode(path: str, **kwargs: Any) -> httpx.Response:
            return httpx.Response(
                500, request=httpx.Request("POST", "https://api.stratz.com/graphql")
            )

        monkeypatch.setattr(client._client, "post", explode)
        monkeypatch.setattr(
            StratzClient, "post_json", BaseClient.post_json.retry_with(stop=stop_after_attempt(1))
        )  # type: ignore[attr-defined]

        with pytest.raises(httpx.HTTPStatusError):
            await client.match(1)

        await client.aclose()
