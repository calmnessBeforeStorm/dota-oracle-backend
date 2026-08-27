"""Source D: Liquipedia (spec sections 2.5, 3, 5.5).

The only authoritative source of Tier 1 marking, the schedule, and - critically - the
series FORMAT: stages carry the default (group stage Bo2, playoff Bo3, grand final Bo5),
and Valve data cannot tell a Bo2 from two Bo1s.

Terms of use are enforced by IP ban, so they are enforced here:
  - custom User-Agent with project name and contact email (mandatory)
  - ~1 request / 2s, and ~1 / 30s for action=parse
  - responses cached on our side; schedule polled at most hourly
  - CC-BY-SA attribution wherever the data is displayed
"""

from typing import Any

from app.core.config import get_settings
from app.ingestion.clients.base import BaseClient


class LiquipediaClient(BaseClient):
    base_url = "https://liquipedia.net/dota2"
    min_interval = 2.0

    #: action=parse is rate limited far more aggressively than action=query
    parse_min_interval = 30.0

    def __init__(self) -> None:
        self.user_agent = get_settings().liquipedia_user_agent
        super().__init__()

    async def query(self, **params: Any) -> dict[str, Any]:
        return await self.get_json("/api.php", action="query", format="json", **params)  # type: ignore[no-any-return]

    async def parse_page(self, page: str, prop: str = "wikitext") -> dict[str, Any]:
        """Wiki source of a tournament page. Stage formats are extracted from here."""
        self.min_interval = self.parse_min_interval
        try:
            return await self.get_json(  # type: ignore[no-any-return]
                "/api.php", action="parse", format="json", page=page, prop=prop
            )
        finally:
            self.min_interval = 2.0

    async def page_wikitext(self, page: str) -> str:
        """Just the source text, or empty when the page does not exist."""
        response = await self.parse_page(page)
        return str(((response.get("parse") or {}).get("wikitext") or {}).get("*", ""))
