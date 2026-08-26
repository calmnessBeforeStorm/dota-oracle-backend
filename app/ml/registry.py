"""Model registry (spec sections 4.3, 9.3, phase 4).

Artifacts live in object storage (MinIO/S3); MLflow tracks experiments. Whatever is served
must be identifiable: `predictions.model_version` is the only way to explain a quality drop
after the fact, so a model with no version never gets served.
"""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class ModelCard:
    version: str
    trained_at: datetime
    train_window: tuple[datetime, datetime]
    patch_range: tuple[int, int] | None
    holdout_log_loss: float
    holdout_brier: float
    # Metrics per minute bucket - the averaged number lies, minute 40 is trivial
    # (spec section 7.2).
    log_loss_by_minute: dict[str, float]
    notes: str = ""


def list_models() -> list[ModelCard]:
    """TODO(phase-4): read model cards from MODEL_DIR / object storage."""
    return []
