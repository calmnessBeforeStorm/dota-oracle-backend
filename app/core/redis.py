"""Redis connection: response cache + pub/sub bus for live updates (spec section 9.2)."""

from collections.abc import AsyncIterator

from redis.asyncio import Redis

from app.core.config import get_settings

_client: Redis | None = None

LIVE_CHANNEL_PREFIX = "live:match:"


def get_redis() -> Redis:
    global _client
    if _client is None:
        _client = Redis.from_url(get_settings().redis_url, decode_responses=True)
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def publish_prediction(match_id: int, payload: str) -> None:
    await get_redis().publish(f"{LIVE_CHANNEL_PREFIX}{match_id}", payload)


#: How long to wait for a message before yielding an idle tick. Without one the loop parks
#: in redis forever, which is fine until the client goes away or the process tries to stop:
#: an open subscription then blocks shutdown, because nothing ever notices.
IDLE_TICK_SECONDS = 5.0


async def subscribe_predictions(match_id: int) -> AsyncIterator[str | None]:
    """Yield predictions for a match, and `None` whenever nothing arrived in a while.

    The idle tick is what lets the caller check that its client is still there. A live match
    can go a minute between updates, and a subscription that cannot be interrupted is a
    subscription that keeps the server from shutting down.
    """
    channel = f"{LIVE_CHANNEL_PREFIX}{match_id}"
    pubsub = get_redis().pubsub()
    await pubsub.subscribe(channel)
    try:
        while True:
            message = await pubsub.get_message(
                ignore_subscribe_messages=True, timeout=IDLE_TICK_SECONDS
            )
            if message is None:
                yield None
            elif message.get("type") == "message":
                yield str(message["data"])
    finally:
        await pubsub.unsubscribe(channel)
        # redis-py ships py.typed, but PubSub.aclose() has no return annotation
        # (redis 8.1.0, redis/asyncio/client.py), so strict mypy rejects the call.
        # close() and reset() are deprecated aliases for it, so aclose() stays correct.
        await pubsub.aclose()  # type: ignore[no-untyped-call]
