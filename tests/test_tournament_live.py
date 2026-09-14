"""The tournaments calendar's live gate (design 2026-09-11-pro-segment, section 2).

`_live_league_ids` feeds both `list_tournaments` and `tournament_detail` (spec section 8.1).
Before this fix it counted every league the poller had on air, so an amateur cup could be
badged "current" on the calendar while `/matches/live` refused to show that same game -
`public_feed` in `app/api/routes/matches.py` already gates the feed on `is_pro`. The
calendar now applies the same gate, so the two cannot disagree about what is running.
"""

from typing import Any

import orjson
import pytest

from app.api.routes import tournaments
from app.ingestion.workers.live_poller import LIVE_FEED_KEY


class _FakeRedis:
    """Only `GET`, which is all `_live_league_ids` uses."""

    def __init__(self, value: str | None) -> None:
        self._value = value

    async def get(self, name: str) -> str | None:
        assert name == LIVE_FEED_KEY
        return self._value


def entry(league_id: int, valve_tier: str | None) -> dict[str, Any]:
    return {"league_id": league_id, "valve_tier": valve_tier}


async def test_an_amateur_league_on_air_does_not_count_as_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feed = orjson.dumps([entry(1, "amateur")]).decode()
    monkeypatch.setattr(tournaments, "get_redis", lambda: _FakeRedis(feed))

    assert await tournaments._live_league_ids() == set()


async def test_an_unknown_tier_league_on_air_does_not_count_as_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feed = orjson.dumps([entry(1, None)]).decode()
    monkeypatch.setattr(tournaments, "get_redis", lambda: _FakeRedis(feed))

    assert await tournaments._live_league_ids() == set()


async def test_a_professional_league_on_air_counts_as_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feed = orjson.dumps(
        [entry(1, "amateur"), entry(2, "professional"), entry(3, "premium")]
    ).decode()
    monkeypatch.setattr(tournaments, "get_redis", lambda: _FakeRedis(feed))

    assert await tournaments._live_league_ids() == {2, 3}


async def test_an_empty_cache_is_no_live_leagues(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tournaments, "get_redis", lambda: _FakeRedis(None))

    assert await tournaments._live_league_ids() == set()
