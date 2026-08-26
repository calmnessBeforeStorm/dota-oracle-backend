"""Live loop (spec sections 2.4, 10, phase 5).

GetLiveLeagueGames every 20-30s -> for each active game, GetRealtimeStats every 10-20s ->
adapt to GameState -> features -> model -> log to `predictions` -> publish to Redis pub/sub,
which the WebSocket endpoint fans out to browsers.
"""

from typing import Any

from app.core.logging import get_logger

log = get_logger(__name__)


async def poll_live_games(ctx: dict[str, Any]) -> int:
    """One tick of the live loop. Returns the number of games seen.

    TODO(phase-5): implement the full chain. The pieces already exist:
      - app.ingestion.clients.steam.SteamClient
      - app.features.adapters.steam.from_realtime_stats
      - app.features.live.build_live_features
      - app.ml.predictor.get_predictor
      - app.core.redis.publish_prediction
    Every raw response goes to raw_live_snapshots first, unconditionally.
    """
    log.info("live_poll.tick", implemented=False)
    return 0
