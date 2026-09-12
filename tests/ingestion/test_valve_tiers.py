"""Keeping Valve's league tiers fresh without spending OpenDota's quota on it.

A new tournament has no tier until `/leagues` is asked again, and until then the feed hides
it. The poller may therefore ask for a refresh every tick - the throttle is what turns that
into at most four calls an hour.
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.reference import League
from app.ingestion.clients.base import RateLimitedError
from app.ingestion.workers.valve_tiers import (
    REFRESH_EVERY_SECONDS,
    REFRESH_VALVE_TIERS_JOB,
    THROTTLE_KEY,
    refresh_valve_tiers,
    refresh_valve_tiers_once,
    request_valve_tier_refresh,
)


class _FakeRedis:
    """Only `SET key value NX EX ttl`, which is all the throttle uses."""

    def __init__(self) -> None:
        self.store: dict[str, tuple[str, int]] = {}

    async def set(self, name: str, value: str, *, nx: bool = False, ex: int | None = None) -> Any:
        if nx and name in self.store:
            return None
        self.store[name] = (value, ex or 0)
        return True


class _Leagues:
    def __init__(self, *, fail: Exception | None = None) -> None:
        self.calls = 0
        self._fail = fail

    async def leagues(self) -> list[dict[str, Any]]:
        self.calls += 1
        if self._fail:
            raise self._fail
        return [{"leagueid": 19696, "name": "DreamLeague Season 29", "tier": "professional"}]


class _Once:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def __aenter__(self) -> AsyncSession:
        return self._session

    async def __aexit__(self, *_: object) -> None:
        return None


class _FakeArq:
    def __init__(self, *, result: object = object(), window_held: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._result = result
        self._held = window_held

    async def exists(self, name: str) -> int:
        return int(self._held and name == THROTTLE_KEY)

    async def enqueue_job(self, name: str, **kwargs: Any) -> object:
        self.calls.append((name, kwargs))
        return self._result


async def test_writes_the_valve_tier(session: AsyncSession) -> None:
    session.add(League(league_id=19696))
    await session.flush()

    written = await refresh_valve_tiers_once(_Leagues(), lambda: _Once(session), _FakeRedis())

    assert written == 1
    tier = await session.scalar(select(League.valve_tier).where(League.league_id == 19696))
    assert tier == "professional"


async def test_a_second_call_inside_the_window_is_skipped(session: AsyncSession) -> None:
    client, redis = _Leagues(), _FakeRedis()

    first = await refresh_valve_tiers_once(client, lambda: _Once(session), redis)
    second = await refresh_valve_tiers_once(client, lambda: _Once(session), redis)

    assert first == 1
    assert second is None
    assert client.calls == 1
    assert redis.store[THROTTLE_KEY][1] == REFRESH_EVERY_SECONDS


async def test_a_refusal_stops_quietly_and_still_holds_the_window(session: AsyncSession) -> None:
    # Retrying a 429 sooner is how an IP ban is earned; the throttle key stays set.
    client, redis = _Leagues(fail=RateLimitedError(60)), _FakeRedis()

    assert await refresh_valve_tiers_once(client, lambda: _Once(session), redis) == 0
    assert await refresh_valve_tiers_once(client, lambda: _Once(session), redis) is None
    assert client.calls == 1


async def test_any_other_failure_does_not_raise(session: AsyncSession) -> None:
    client = _Leagues(fail=RuntimeError("boom"))
    assert await refresh_valve_tiers_once(client, lambda: _Once(session), _FakeRedis()) == 0


async def test_the_request_uses_a_fixed_job_id() -> None:
    # A fixed id is what stops thirty ticks from queueing thirty refreshes.
    arq = _FakeArq()
    assert await request_valve_tier_refresh({"redis": arq}) is True
    assert arq.calls == [(REFRESH_VALVE_TIERS_JOB, {"_job_id": REFRESH_VALVE_TIERS_JOB})]


async def test_an_already_queued_request_is_reported_as_not_queued() -> None:
    assert await request_valve_tier_refresh({"redis": _FakeArq(result=None)}) is False


async def test_a_closed_window_is_not_even_queued() -> None:
    # Without this, a league missing from `/leagues` queues the job every tick: 120 runs an
    # hour, each logging "skipped" - a light that is always on, which hides the real one.
    arq = _FakeArq(window_held=True)
    assert await request_valve_tier_refresh({"redis": arq}) is False
    assert arq.calls == []


async def test_no_redis_in_the_context_is_not_an_error() -> None:
    assert await request_valve_tier_refresh({}) is False


def test_the_job_id_is_the_function_name() -> None:
    # arq resolves a queued job by function name; the two must not drift apart.
    assert refresh_valve_tiers.__name__ == REFRESH_VALVE_TIERS_JOB
