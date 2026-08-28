"""The STRATZ client's single-match call (spec section 2.3).

The query text is asserted field by field on purpose. A field dropped from it does not
fail here - it fails much later, inside the adapter, on some matches and not others.
"""

from types import SimpleNamespace
from typing import Any

import pytest

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
