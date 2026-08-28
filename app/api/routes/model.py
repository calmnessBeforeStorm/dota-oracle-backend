"""F6: public accuracy dashboard (spec section 8.1).

Not cosmetic - without a visible calibration curve there is no reason to trust the product.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.accuracy import load_scored, metrics_from, scored_versions
from app.db.session import get_session
from app.ml.predictor import get_predictor
from app.schemas.common import ModelMetrics

router = APIRouter(prefix="/model", tags=["model"])


@router.get("/metrics", response_model=ModelMetrics)
async def model_metrics(
    version: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> ModelMetrics:
    """Calibration of served predictions, scored against how the matches actually ended.

    Defaults to the version currently being served rather than to whichever has the most
    data. When that version has nothing scored yet the page shows an empty dashboard, and
    that is the honest answer: an older model's calibration says nothing about the numbers
    a visitor is looking at right now. The other versions are listed so they stay reachable.
    """
    versions = await scored_versions(session)
    wanted = version or get_predictor().version
    metrics = metrics_from(wanted, await load_scored(session, wanted))
    metrics.versions = versions
    return metrics
