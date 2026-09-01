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
from app.ml.calibration import PlattCalibrator
from app.ml.registry import load_card

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
    """Logistic regression on the time-normalised gold lead and the minute.

    Not a real model - a floor. It exists so the whole live loop (poller -> features ->
    prediction -> WebSocket -> UI) can be built and tested before phase 4 delivers weights.

    The coefficients are fitted, not guessed: gradient descent over 21199 snapshots from 491
    matches (2026-07-07 .. 2026-08-09), scored on 140 later matches it never saw.

    **It used to read raw `gold_adv`, and had the sign of its time term backwards.** The old
    formula was `0.055 + gold_adv * (0.00018 + 0.000012 * minute)`, which makes a lead worth
    *more* the later it appears. The data says the opposite, and by a wide margin: a lead
    above 8k wins 98.0% of the time in minutes 10-20 and 82.9% after minute 40. So the model
    claimed 99% where the leading side went on to win two games in three.

    On the same holdout, corrected against original:

        log loss   0.5377 against 0.8514
        ECE        0.0564 against 0.1025
        minute 30+ 0.454  against 1.299 - worse than a coin flip, which scores 0.693
        accuracy   0.732  against 0.727 - which is why section 7.2 forbids judging by it

    `gold_adv_norm` was in the feature vector the whole time. Nothing had to be added; the
    predictor was reading the wrong key out of a dictionary that already held the right one.
    """

    version = "baseline-logistic-0.2"

    def predict_proba_radiant(self, features: dict[str, float]) -> float:
        gold_adv_norm = features.get("gold_adv_norm", 0.0)
        minute = features.get("minute", 0.0)
        # The minute term is negative on purpose: with the lead already normalised, a long
        # game is one neither side has closed out, and those end closer to even.
        z = 0.067223 + 0.005293 * gold_adv_norm - 0.003425 * minute
        return 1.0 / (1.0 + math.exp(-max(min(z, 20.0), -20.0)))


class LightGBMPredictor:
    """Loads a LightGBM booster from MODEL_DIR and the calibration fitted alongside it.

    Requires the `ml` extra. **The calibrator is part of the model, not a reporting detail.**
    Training fits it on the validation slice and every number on the model card - the holdout
    log loss, the ECE, the gate verdict - is measured after it is applied. Loading the booster
    alone would serve something that had never been scored, and it would look completely
    normal: same features, same version string, probabilities in the right range.

    Today the gap happens to be small (measured on `lgbm-20260901-080345`: a=1.0134,
    b=-0.0048, log loss 0.5252 raw against 0.5253 calibrated). That is a property of this
    model, not of the design - `pipeline` records that isotonic once turned a holdout log loss
    of 0.5685 into 0.6115 - so it is not a reason to leave the transform on the floor.

    A card with no coefficients describes the identity, which is what older runs effectively
    served.
    """

    def __init__(self, model_path: Path, version: str, calibrator: PlattCalibrator) -> None:
        import lightgbm as lgb  # imported lazily: the API image does not ship the ml extra

        self.version = version
        self._booster = lgb.Booster(model_file=str(model_path))
        self._calibrator = calibrator

    def predict_proba_radiant(self, features: dict[str, float]) -> float:
        raw = float(self._booster.predict([as_vector(features)])[0])
        return self._calibrator.apply_one(raw)


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
                card = load_card(settings.model_dir, version)
                calibrator = PlattCalibrator(a=card.calibrator_a, b=card.calibrator_b)
                _predictor = _Bounded(LightGBMPredictor(model_path, version, calibrator))
                log.info(
                    "model.loaded",
                    version=version,
                    calibrator=card.calibrator,
                    passes_gate=card.passes_gate,
                )
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
