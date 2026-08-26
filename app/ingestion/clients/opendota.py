"""Source A: OpenDota (spec section 2.2). Main dataset source, 50k calls/month, 60 req/min."""

from typing import Any

from app.core.config import get_settings
from app.ingestion.clients.base import BaseClient


class OpenDotaClient(BaseClient):
    base_url = "https://api.opendota.com/api"
    min_interval = 1.05  # 60 req/min with a margin

    def __init__(self) -> None:
        super().__init__()
        self._api_key = get_settings().opendota_api_key

    async def get_json(self, path: str, **params: Any) -> Any:
        if self._api_key:
            params["api_key"] = self._api_key
        return await super().get_json(path, **params)

    async def pro_matches(self, less_than_match_id: int | None = None) -> list[dict[str, Any]]:
        """~100 matches per call. Paginate backwards through history with less_than_match_id."""
        return await self.get_json("/proMatches", less_than_match_id=less_than_match_id)  # type: ignore[no-any-return]

    async def match(self, match_id: int) -> dict[str, Any]:
        """One call per match. `version is None` means unparsed: no per-minute series."""
        return await self.get_json(f"/matches/{match_id}")  # type: ignore[no-any-return]

    async def leagues(self) -> list[dict[str, Any]]:
        """`tier` here (premium/professional/amateur) is only a fallback for Tier 1 marking."""
        return await self.get_json("/leagues")  # type: ignore[no-any-return]

    async def pro_players(self) -> list[dict[str, Any]]:
        return await self.get_json("/proPlayers")  # type: ignore[no-any-return]

    async def team_players(self, team_id: int) -> list[dict[str, Any]]:
        return await self.get_json(f"/teams/{team_id}/players")  # type: ignore[no-any-return]
