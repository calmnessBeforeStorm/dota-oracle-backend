"""Login attempt counters and locks in Redis.

Two counters per attempt: by login, which stops guessing one account from many addresses, and by
address, which stops guessing many accounts from one. The address is whatever uvicorn put in
`request.client` - see the comment on `--forwarded-allow-ips` in Dockerfile.prod for why that is
only trustworthy while the API port stays unpublished, and the one next to the /api proxy in
deploy/Caddyfile for what a proxy in front of Caddy would do to it.

An attempt is counted before the password is checked, not after a failure. Argon2 takes tens of
milliseconds in a thread, and counting after it let every request that arrived meanwhile pass the
lock check: forty simultaneous wrong passwords ran forty hash verifications at a limit of five.
A success then takes its own attempt back, so the counters still measure failures.
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


async def count_attempt(redis: Redis, *, username: str, ip: str) -> int | None:
    """Count an attempt against both counters; lock and return the lock's seconds when either
    counter is now over its limit, otherwise None.

    Strictly over: the limit is the number of failures allowed, so a correct password on exactly
    the LOGIN_LIMIT-th attempt still gets in. The counter is not deleted when the lock is set - a
    request already in flight would increment a fresh key from 1; it expires with its window.
    """
    locked = False
    for kind, value, limit in (("login", username, LOGIN_LIMIT), ("ip", ip, IP_LIMIT)):
        key = failure_key(kind, value)
        count = int(await redis.incr(key))
        # NX: the window is fixed at the first attempt, and a counter that lost its expiry to a
        # crash between the two calls gets one back on the next attempt instead of never.
        await redis.expire(key, WINDOW_SECONDS, nx=True)
        if count > limit:
            await redis.set(lock_key(kind, value), "1", ex=LOCK_SECONDS)
            locked = True
    return LOCK_SECONDS if locked else None


async def forgive_success(redis: Redis, *, username: str, ip: str) -> None:
    """Take back the attempt a successful login counted.

    The login counter is cleared: the owner got in. The address counter only loses this one
    attempt - one good password from an address that has been guessing other accounts says
    nothing about those guesses. DECR on a counter that expired meanwhile creates it at -1 with
    no expiry; EXPIRE NX gives it a window, so it cannot outlive one.
    """
    await redis.delete(failure_key("login", username))
    key = failure_key("ip", ip)
    await redis.decr(key)
    await redis.expire(key, WINDOW_SECONDS, nx=True)
