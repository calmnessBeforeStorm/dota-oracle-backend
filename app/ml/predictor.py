"""Inference (spec section 9.3): the model is loaded into the FastAPI process. LightGBM
scores in microseconds, so a separate model service would be pure overhead.

Until a trained model exists, the baseline below is served. It is deliberately the same
logistic-on-gold_adv-and-minute baseline the real model must beat (spec section 7.3): if
LightGBM cannot clearly beat this, there is a bug somewhere.
"""

import math
from pathlib import Path
from typing import Protocol

from app.core.config import get_settings
from app.core.logging import get_logger
from app.features.live import as_vector

log = get_logger(__name__)


class Predictor(Protocol):
    version: str

    def predict_proba_radiant(self, features: dict[str, float]) -> float: ...


#: Nothing is ever served outside this range.
#:
#: Measured on our own prediction log, 2026-08-28: of the 86 finished matches where the
#: baseline claimed at least 99.9% for a side, that side lost 8 times. A claim of 99.9% that
#: is wrong once in eleven matches is not a probability, it is a rounding error with a
#: decimal point.
#:
#: The cost is not only in credibility. The baseline clamps its logit at +-20, so a confident
#: miss costs 20 nats where an ordinary row costs 0.5 - one comeback then decides the log
#: loss of an entire minute bucket, which is exactly what happened to the 30+ bucket on the
#: accuracy dashboard.
#:
#: This is a guard, not calibration. It does not make the probabilities honest - only a
#: fitted model and a calibration step do that (phase 4). It stops us from making a claim no
#: amount of gold advantage can support: in Dota the losing side keeps its buyback, its
#: ancient and every comeback that has ever happened.
SERVING_BOUNDS = (0.01, 0.99)


def bounded(p: float) -> float:
    low, high = SERVING_BOUNDS
    return min(max(p, low), high)


class _Bounded:
    """Applies `bounded` to whatever predictor is active.

    Wrapped here rather than inside each predictor so the rule survives the model being
    replaced: phase 4 swaps the booster in, and a rule written into the placeholder would
    have left with it.
    """

    def __init__(self, inner: Predictor) -> None:
        self._inner = inner
        self.version = inner.version

    def predict_proba_radiant(self, features: dict[str, float]) -> float:
        return bounded(self._inner.predict_proba_radiant(features))


class BaselinePredictor:
    """Logistic regression on gold advantage and minute. Hand-fitted placeholder values.

    Not a real model - a floor. It exists so the whole live loop (poller -> features ->
    prediction -> WebSocket -> UI) can be built and tested before phase 4 delivers weights.
    """

    version = "baseline-logistic-0.1"

    def predict_proba_radiant(self, features: dict[str, float]) -> float:
        gold_adv = features.get("gold_adv", 0.0)
        minute = features.get("minute", 0.0)
        # Gold leads matter more the later they appear; 0.055 is the Radiant side bias.
        z = 0.055 + gold_adv * (0.00018 + 0.000012 * minute)
        return 1.0 / (1.0 + math.exp(-max(min(z, 20.0), -20.0)))


class LightGBMPredictor:
    """Loads a LightGBM booster from MODEL_DIR. Requires the `ml` extra."""

    def __init__(self, model_path: Path, version: str) -> None:
        import lightgbm as lgb  # imported lazily: the API image does not ship the ml extra

        self.version = version
        self._booster = lgb.Booster(model_file=str(model_path))

    def predict_proba_radiant(self, features: dict[str, float]) -> float:
        return float(self._booster.predict([as_vector(features)])[0])


_predictor: Predictor | None = None


def get_predictor() -> Predictor:
    """Resolve the active model, falling back to the baseline so the service always answers."""
    global _predictor
    if _predictor is not None:
        return _predictor

    settings = get_settings()
    version = settings.active_model_version
    if version:
        model_path = Path(settings.model_dir) / f"{version}.txt"
        if model_path.exists():
            try:
                _predictor = _Bounded(LightGBMPredictor(model_path, version))
                log.info("model.loaded", version=version)
                return _predictor
            except Exception as exc:
                log.error("model.load_failed", version=version, error=str(exc))
        else:
            log.warning("model.missing", path=str(model_path))

    log.warning("model.baseline_fallback")
    _predictor = _Bounded(BaselinePredictor())
    return _predictor


def reset_predictor() -> None:
    """Drop the cached predictor - used by tests and by model hot-swap."""
    global _predictor
    _predictor = None
