"""Login lockout (design 2026-09-11-auth-and-pipeline-panel, section 1)."""

from app.auth.lockout import (
    IP_LIMIT,
    LOCK_SECONDS,
    LOGIN_LIMIT,
    WINDOW_SECONDS,
    count_attempt,
    failure_key,
    forgive_success,
    lock_key,
    retry_after,
)
from tests.auth.conftest import FakeRedis


def test_limits_follow_the_spec() -> None:
    assert (LOGIN_LIMIT, IP_LIMIT) == (5, 20)
    assert WINDOW_SECONDS == LOCK_SECONDS == 15 * 60


async def test_nothing_is_locked_at_first(fake_redis: FakeRedis) -> None:
    assert await retry_after(fake_redis, username="adilet", ip="10.0.0.1") is None


async def test_attempts_up_to_the_limit_do_not_lock(fake_redis: FakeRedis) -> None:
    """The limit is the number of attempts allowed: the LOGIN_LIMIT-th may still be the owner."""
    for i in range(LOGIN_LIMIT):
        assert await count_attempt(fake_redis, username="adilet", ip=f"10.0.0.{i}") is None
    assert await retry_after(fake_redis, username="adilet", ip="10.0.0.99") is None


async def test_the_attempt_over_the_login_limit_locks_that_login_from_any_address(
    fake_redis: FakeRedis,
) -> None:
    for i in range(LOGIN_LIMIT):
        await count_attempt(fake_redis, username="adilet", ip=f"10.0.0.{i}")
    assert await count_attempt(fake_redis, username="adilet", ip="10.0.0.50") == LOCK_SECONDS
    assert await retry_after(fake_redis, username="adilet", ip="10.0.0.99") == LOCK_SECONDS
    assert await retry_after(fake_redis, username="someone", ip="10.0.0.99") is None


async def test_the_attempt_over_the_ip_limit_locks_every_login_from_that_address(
    fake_redis: FakeRedis,
) -> None:
    for i in range(IP_LIMIT):
        assert await count_attempt(fake_redis, username=f"guess-{i}", ip="10.0.0.1") is None
    assert await count_attempt(fake_redis, username="guess-last", ip="10.0.0.1") == LOCK_SECONDS
    assert await retry_after(fake_redis, username="anyone", ip="10.0.0.1") == LOCK_SECONDS
    assert await retry_after(fake_redis, username="anyone", ip="10.0.0.2") is None


async def test_locking_keeps_the_counter(fake_redis: FakeRedis) -> None:
    """Deleting it would let requests already in flight start counting again from one."""
    for _ in range(LOGIN_LIMIT + 1):
        await count_attempt(fake_redis, username="adilet", ip="10.0.0.1")
    assert fake_redis.values[lock_key("login", "adilet")] == "1"
    assert fake_redis.values[failure_key("login", "adilet")] == str(LOGIN_LIMIT + 1)


async def test_the_window_starts_at_the_first_attempt(fake_redis: FakeRedis) -> None:
    """A later attempt must not push the window out, or slow guessing never resets."""
    await count_attempt(fake_redis, username="adilet", ip="10.0.0.1")
    key = failure_key("login", "adilet")
    assert fake_redis.ttls[key] == WINDOW_SECONDS
    fake_redis.ttls[key] = 100
    await count_attempt(fake_redis, username="adilet", ip="10.0.0.1")
    assert fake_redis.ttls[key] == 100


async def test_a_counter_left_without_a_window_gets_one(fake_redis: FakeRedis) -> None:
    """A crash between INCR and EXPIRE would otherwise leave a counter that never expires."""
    key = failure_key("login", "adilet")
    fake_redis.values[key] = "2"
    await count_attempt(fake_redis, username="adilet", ip="10.0.0.1")
    assert fake_redis.ttls[key] == WINDOW_SECONDS


async def test_success_clears_the_login_counter_and_takes_back_one_address_attempt(
    fake_redis: FakeRedis,
) -> None:
    await count_attempt(fake_redis, username="someone", ip="10.0.0.1")  # a failure
    await count_attempt(fake_redis, username="adilet", ip="10.0.0.1")  # the success
    await forgive_success(fake_redis, username="adilet", ip="10.0.0.1")
    assert failure_key("login", "adilet") not in fake_redis.values
    assert fake_redis.values[failure_key("login", "someone")] == "1"
    assert fake_redis.values[failure_key("ip", "10.0.0.1")] == "1"
    assert fake_redis.ttls[failure_key("ip", "10.0.0.1")] == WINDOW_SECONDS


async def test_success_after_the_address_window_expired_leaves_no_eternal_counter(
    fake_redis: FakeRedis,
) -> None:
    await forgive_success(fake_redis, username="adilet", ip="10.0.0.1")
    assert fake_redis.ttls[failure_key("ip", "10.0.0.1")] == WINDOW_SECONDS
