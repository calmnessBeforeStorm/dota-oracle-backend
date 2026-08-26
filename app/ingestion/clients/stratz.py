"""Source B: STRATZ GraphQL (spec section 2.3).

Bulk backfill: one GraphQL call returns up to ~100 matches with all players, where OpenDota
spends one call per match. STRATZ gives the skeleton of history cheaply; the per-minute
series (radiant_gold_adv etc.) still come from OpenDota, fetched only for matches that make
it into the training set.
"""

from typing import Any

from app.core.config import get_settings
from app.ingestion.clients.base import BaseClient

LEAGUE_MATCHES_QUERY = """
query LeagueMatches($leagueId: Int!, $take: Int!, $skip: Int!) {
  league(id: $leagueId) {
    id
    displayName
    matches(request: {take: $take, skip: $skip}) {
      id
      didRadiantWin
      durationSeconds
      startDateTime
      radiantTeam { id name }
      direTeam { id name }
      players { steamAccountId heroId isRadiant kills deaths assists networth position }
    }
  }
}
"""


class StratzClient(BaseClient):
    base_url = "https://api.stratz.com"
    min_interval = 2.0  # ~2000 req/hour on the default tier

    def __init__(self) -> None:
        super().__init__()
        token = get_settings().stratz_api_token
        if not token:
            raise RuntimeError("STRATZ_API_TOKEN is required for the STRATZ backfill")
        self._client.headers["Authorization"] = f"Bearer {token}"

    async def query(self, query: str, **variables: Any) -> dict[str, Any]:
        data = await self.post_json("/graphql", {"query": query, "variables": variables})
        if data.get("errors"):
            raise RuntimeError(f"STRATZ returned errors: {data['errors']}")
        return dict(data.get("data", {}))

    async def league_matches(
        self, league_id: int, take: int = 100, skip: int = 0
    ) -> list[dict[str, Any]]:
        data = await self.query(LEAGUE_MATCHES_QUERY, leagueId=league_id, take=take, skip=skip)
        return list((data.get("league") or {}).get("matches") or [])
