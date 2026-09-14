"""Failed-login counters and locks in Redis.

Two counters per attempt: by login, which stops guessing one account from many addresses, and by
address, which stops guessing many accounts from one. The address is whatever uvicorn put in
`request.client` - see the comment on `--forwarded-allow-ips` in Dockerfile.prod for why that is
only trustworthy while the API port stays unpublished.
"""

from redis.asyncio import Redis

LOGIN_LIMIT = 5
IP_LIMIT = 20
WINDOW_SECONDS = 15 * 60
LOCK_SECONDS = 15 * 60


def failure_key(kind: str, value: str) -> str:
    return f"auth:failures:{kind}:{value}"


def lock_key(kind: str, value: str) -> str:
    return f"auth:lock:{kind}:{value}"


async def retry_after(redis: Redis, *, username: str, ip: str) -> int | None:
    """Seconds until the longest active lock ends, or None when neither is locked."""
    # int(): redis-py types command results as Any, and mypy strict refuses to return Any.
    remaining = [
        int(await redis.ttl(lock_key("login", username))),
        int(await redis.ttl(lock_key("ip", ip))),
    ]
    # TTL answers -2 for a missing key and -1 for one without expiry; neither is a lock here.
    active = [seconds for seconds in remaining if seconds > 0]
    return max(active) if active else None


async def record_failure(redis: Redis, *, username: str, ip: str) -> None:
    for kind, value, limit in (("login", username, LOGIN_LIMIT), ("ip", ip, IP_LIMIT)):
        key = failure_key(kind, value)
        count = await redis.incr(key)
        # NX: the window is fixed at the first failure, and a counter that lost its expiry to a
        # crash between the two calls gets one back on the next failure instead of never.
        await redis.expire(key, WINDOW_SECONDS, nx=True)
        if count >= limit:
            await redis.set(lock_key(kind, value), "1", ex=LOCK_SECONDS)
            await redis.delete(key)


async def reset_login(redis: Redis, *, username: str) -> None:
    """After a successful login. The address counter stays: one good password from an address
    that has been guessing other accounts says nothing about those guesses."""
    await redis.delete(failure_key("login", username))
