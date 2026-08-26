"""Source C: Steam Web API (spec section 2.4).

The only free channel of live game state. Without it the core product does not work.
"""

from typing import Any

from app.core.config import get_settings
from app.ingestion.clients.base import BaseClient


class SteamClient(BaseClient):
    base_url = "https://api.steampowered.com"
    min_interval = 0.2

    def __init__(self) -> None:
        super().__init__()
        settings = get_settings()
        if not settings.steam_api_key:
            raise RuntimeError("STEAM_API_KEY is required for the live loop")
        self._key = settings.steam_api_key

    async def live_league_games(self) -> list[dict[str, Any]]:
        """Poll every 20-30s. Gives server_steam_id (needed for C2), series score and
        stream_delay_s - the broadcast delay the UI must surface (spec section 7.4)."""
        data = await self.get_json("/IDOTA2Match_570/GetLiveLeagueGames/v1/", key=self._key)
        return list(data.get("result", {}).get("games", []))

    async def realtime_stats(self, server_steam_id: int) -> dict[str, Any]:
        """Poll every 10-20s per active game. This is the payload the live features are
        built from, and the one that must stay isomorphic to the training features."""
        return await self.get_json(  # type: ignore[no-any-return]
            "/IDOTA2MatchStats_570/GetRealtimeStats/v1/",
            key=self._key,
            server_steam_id=server_steam_id,
        )

    async def match_details(self, match_id: int) -> dict[str, Any]:
        """Fallback for final results when OpenDota lags behind."""
        return await self.get_json(  # type: ignore[no-any-return]
            "/IDOTA2Match_570/GetMatchDetails/v1/", key=self._key, match_id=match_id
        )
