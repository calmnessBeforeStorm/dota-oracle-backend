"""F6: public accuracy dashboard (spec section 8.1).

Not cosmetic - without a visible calibration curve there is no reason to trust the product.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.accuracy import load_scored, metrics_from, scored_versions, serving_progress
from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.session import get_session
from app.ml.predictor import get_predictor
from app.ml.registry import ModelCard, load_card
from app.schemas.common import ModelMetrics, ModelTraining

log = get_logger(__name__)


def _training(version: str) -> ModelTraining | None:
    """The model card, in public form, or None for a version that has no artifact.

    The baselines have none and never will: they are code rather than something anybody fitted
    and held a slice back from. Returning None rather than zeroes keeps that distinction -
    a baseline with `holdout_matches: 0` would read as a model that failed validation.
    """
    try:
        card: ModelCard = load_card(get_settings().model_dir, version)
    except (OSError, ValueError, TypeError) as exc:
        log.info("model.card_unavailable", version=version, error=str(exc))
        return None

    return ModelTraining(
        trained_at=card.trained_at,
        train_matches=card.train_matches,
        train_rows=card.train_rows,
        train_window=list(card.train_window),
        holdout_matches=card.holdout_matches,
        holdout_rows=card.holdout_rows,
        holdout_window=list(card.holdout_window),
        holdout_log_loss=card.holdout_log_loss,
        holdout_brier=card.holdout_brier,
        holdout_ece=card.holdout_ece,
        passes_gate=card.passes_gate,
        gate_failures=list(card.gate_failures),
        gate_ties=list(card.gate_ties),
        calibrator=card.calibrator,
        weighted=card.weighted,
        feature_count=len(card.feature_order),
    )


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

    progress = await serving_progress(session, wanted)
    metrics.predicted_matches = progress.predicted_matches
    metrics.awaiting_outcome = max(progress.predicted_matches - metrics.matches, 0)
    metrics.first_prediction_at = progress.first_prediction_at
    metrics.last_prediction_at = progress.last_prediction_at
    metrics.training = _training(wanted)
    return metrics
