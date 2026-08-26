"""F1/F2: live feed and match card (spec section 8.1)."""

from fastapi import APIRouter, HTTPException

from app.ml.predictor import get_predictor
from app.schemas.common import LiveMatch, MatchDetail

router = APIRouter(prefix="/matches", tags=["matches"])


@router.get("/live", response_model=list[LiveMatch])
async def live_matches() -> list[LiveMatch]:
    """TODO(phase-5): read the live cache Redis is fed by the poller."""
    _ = get_predictor()
    return []


@router.get("/{match_id}", response_model=MatchDetail)
async def match_detail(match_id: int) -> MatchDetail:
    """TODO(phase-5): match + probability curve from `predictions`."""
    raise HTTPException(status_code=404, detail="not implemented yet")
