"""Teams and players. v2 surface (spec section 8.2), stubbed in v1."""

from fastapi import APIRouter

router = APIRouter(prefix="/teams", tags=["teams"])


@router.get("/{team_id}")
async def team_detail(team_id: int) -> dict[str, object]:
    """TODO(v2): history, roster, rating."""
    return {"team_id": team_id}
