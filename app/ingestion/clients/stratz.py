"""Source B: STRATZ GraphQL (spec section 2.3).

Since 27.08.2026 this is the primary source of per-minute series, not a bulk-backfill
shortcut. Two reasons, both measured (see
docs/superpowers/specs/2026-08-27-stratz-adapter-design.md):

  - OpenDota's daily allowance, not its monthly one, is what stops a run - about 700 maps
    a day without a key. STRATZ allows ~2000 requests an hour.
  - OpenDota's per-minute series is *earned gold*; STRATZ's is *net worth*, which is the
    quantity the live scoreboard reports. Training on STRATZ is what makes the offline and
    live paths describe the same thing.

Two limits worth knowing before reaching for a bigger query. The bulk `matches(ids:)` form
the spec assumed needs an admin token ("User is not an admin"), so matches are fetched one
at a time. And on our token every list under `playbackData` comes back empty, which is why
buildings are read from `towerDeaths` instead.
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


#: Everything the train-time adapter reads, and nothing else. `playbackData` is deliberately
#: absent: on our token its lists come back empty, so asking for it costs response size and
#: returns nothing. The end-of-match masks (`towerStatusRadiant` and friends) are absent for
#: a stronger reason - they describe the final state, and a query that does not carry them
#: cannot leak them into a minute-15 row (spec section 12).
MATCH_QUERY = """
query Match($id: Long!) {
  match(id: $id) {
    id didRadiantWin durationSeconds startDateTime endDateTime parsedDateTime
    leagueId radiantTeamId direTeamId gameVersionId
    radiantNetworthLeads radiantExperienceLeads radiantKills direKills
    pickBans { isPick heroId bannedHeroId isRadiant order }
    players {
      steamAccountId heroId isRadiant playerSlot
      kills deaths assists numLastHits numDenies
      networth goldPerMinute experiencePerMinute leaverStatus lane
      stats { networthPerMinute }
    }
    towerDeaths { time npcId isRadiant }
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

    async def match(self, match_id: int) -> dict[str, Any]:
        """One call per map. This is where the per-minute series live.

        An unknown id comes back as a null match rather than an error, so it is reported
        as an empty payload and left for the caller to skip.
        """
        data = await self.query(MATCH_QUERY, id=match_id)
        return dict(data.get("match") or {})

    async def league_matches(
        self, league_id: int, take: int = 100, skip: int = 0
    ) -> list[dict[str, Any]]:
        data = await self.query(LEAGUE_MATCHES_QUERY, leagueId=league_id, take=take, skip=skip)
        return list((data.get("league") or {}).get("matches") or [])
