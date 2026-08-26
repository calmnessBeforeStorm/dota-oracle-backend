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


async def subscribe_predictions(match_id: int) -> AsyncIterator[str]:
    pubsub = get_redis().pubsub()
    await pubsub.subscribe(f"{LIVE_CHANNEL_PREFIX}{match_id}")
    try:
        async for message in pubsub.listen():
            if message["type"] == "message":
                yield str(message["data"])
    finally:
        await pubsub.unsubscribe(f"{LIVE_CHANNEL_PREFIX}{match_id}")
        await pubsub.aclose()
