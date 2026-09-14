"""Login lockout (design 2026-09-11-auth-and-pipeline-panel, section 1)."""

from app.auth.lockout import (
    IP_LIMIT,
    LOCK_SECONDS,
    LOGIN_LIMIT,
    WINDOW_SECONDS,
    failure_key,
    record_failure,
    reset_login,
    retry_after,
)
from tests.auth.conftest import FakeRedis


def test_limits_follow_the_spec() -> None:
    assert (LOGIN_LIMIT, IP_LIMIT) == (5, 20)
    assert WINDOW_SECONDS == LOCK_SECONDS == 15 * 60


async def test_nothing_is_locked_at_first(fake_redis: FakeRedis) -> None:
    assert await retry_after(fake_redis, username="adilet", ip="10.0.0.1") is None


async def test_one_failure_short_of_the_limit_does_not_lock(fake_redis: FakeRedis) -> None:
    for i in range(LOGIN_LIMIT - 1):
        await record_failure(fake_redis, username="adilet", ip=f"10.0.0.{i}")
    assert await retry_after(fake_redis, username="adilet", ip="10.0.0.99") is None


async def test_the_login_limit_locks_that_login_from_any_address(fake_redis: FakeRedis) -> None:
    for i in range(LOGIN_LIMIT):
        await record_failure(fake_redis, username="adilet", ip=f"10.0.0.{i}")
    assert await retry_after(fake_redis, username="adilet", ip="10.0.0.99") == LOCK_SECONDS
    assert await retry_after(fake_redis, username="someone", ip="10.0.0.99") is None


async def test_the_ip_limit_locks_every_login_from_that_address(fake_redis: FakeRedis) -> None:
    for i in range(IP_LIMIT):
        await record_failure(fake_redis, username=f"guess-{i}", ip="10.0.0.1")
    assert await retry_after(fake_redis, username="anyone", ip="10.0.0.1") == LOCK_SECONDS
    assert await retry_after(fake_redis, username="anyone", ip="10.0.0.2") is None


async def test_the_window_starts_at_the_first_failure(fake_redis: FakeRedis) -> None:
    """A later failure must not push the window out, or slow guessing never resets."""
    await record_failure(fake_redis, username="adilet", ip="10.0.0.1")
    key = failure_key("login", "adilet")
    assert fake_redis.ttls[key] == WINDOW_SECONDS
    fake_redis.ttls[key] = 100
    await record_failure(fake_redis, username="adilet", ip="10.0.0.1")
    assert fake_redis.ttls[key] == 100


async def test_a_counter_left_without_a_window_gets_one(fake_redis: FakeRedis) -> None:
    """A crash between INCR and EXPIRE would otherwise leave a counter that never expires."""
    key = failure_key("login", "adilet")
    fake_redis.values[key] = "2"
    await record_failure(fake_redis, username="adilet", ip="10.0.0.1")
    assert fake_redis.ttls[key] == WINDOW_SECONDS


async def test_reset_clears_the_login_counter_but_not_the_address(fake_redis: FakeRedis) -> None:
    await record_failure(fake_redis, username="adilet", ip="10.0.0.1")
    await reset_login(fake_redis, username="adilet")
    assert failure_key("login", "adilet") not in fake_redis.values
    assert fake_redis.values[failure_key("ip", "10.0.0.1")] == "1"
