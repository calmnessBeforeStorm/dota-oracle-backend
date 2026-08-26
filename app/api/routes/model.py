"""F6: public accuracy dashboard (spec section 8.1).

Not cosmetic - without a visible calibration curve there is no reason to trust the product.
"""

from fastapi import APIRouter

from app.ml.predictor import get_predictor
from app.schemas.common import ModelMetrics

router = APIRouter(prefix="/model", tags=["model"])


@router.get("/metrics", response_model=ModelMetrics)
async def model_metrics() -> ModelMetrics:
    """TODO(phase-4): compute from `predictions` joined with final results."""
    return ModelMetrics(
        model_version=get_predictor().version,
        log_loss_by_minute={},
        brier_by_minute={},
        ece=0.0,
        sample_size=0,
    )
