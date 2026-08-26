"""F3/F4: tournament calendar and page (spec section 8.1).

Any page rendering Liquipedia-derived data must carry visible CC-BY-SA attribution
(spec section 13).
"""

from fastapi import APIRouter, Query

router = APIRouter(prefix="/tournaments", tags=["tournaments"])


@router.get("")
async def list_tournaments(
    status: str = Query(default="current", pattern="^(current|upcoming|past)$"),
    tier: str | None = None,
    region: str | None = None,
) -> list[dict[str, object]]:
    """TODO(phase-2): serve from `leagues` + `tournament_stages`."""
    return []


@router.get("/{league_id}")
async def tournament_detail(league_id: int) -> dict[str, object]:
    """TODO(phase-2): bracket, participants, schedule, results.

    Stages carry `default_format`, so Bo2 group stages render three-way series scores
    correctly instead of being guessed from Valve data (spec section 5.5).
    """
    return {"league_id": league_id, "stages": []}
