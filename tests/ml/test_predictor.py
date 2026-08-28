"""What the service is allowed to say out loud (spec sections 7.4, 9.3).

The rule under test is not about model quality. It is about the one claim a live predictor
must never make: certainty. In Dota the losing side keeps its buyback and its ancient, and
every comeback that has ever happened started from a position some number called hopeless.
"""

import math

from app.ml.predictor import (
    SERVING_BOUNDS,
    BaselinePredictor,
    _Bounded,
    bounded,
    get_predictor,
    reset_predictor,
)

LOW, HIGH = SERVING_BOUNDS


class Certain:
    """A predictor with no doubts, which is what an unclamped logistic becomes."""

    version = "certain-1.0"

    def __init__(self, p: float) -> None:
        self.p = p

    def predict_proba_radiant(self, features: dict[str, float]) -> float:
        return self.p


class TestBounds:
    def test_certainty_is_pulled_back(self) -> None:
        assert bounded(1.0) == HIGH
        assert bounded(0.0) == LOW

    def test_ordinary_probabilities_pass_through(self) -> None:
        for p in (0.5, 0.32, 0.87):
            assert bounded(p) == p

    def test_the_bounds_themselves_are_reachable(self) -> None:
        assert bounded(LOW) == LOW
        assert bounded(HIGH) == HIGH

    def test_a_confident_miss_becomes_survivable(self) -> None:
        """The measured damage this exists to stop. The baseline clamps its logit at +-20,
        so being wrong there costs 20 nats - one comeback then decides the log loss of a
        whole minute bucket, which is what happened to 30+ on the accuracy dashboard."""
        unbounded = -math.log(1.0 / (1.0 + math.exp(20.0)))
        clamped = -math.log(1.0 - bounded(1.0))

        assert unbounded > 19
        assert clamped < 5


class TestWrapping:
    def test_it_wraps_whatever_is_inside(self) -> None:
        assert _Bounded(Certain(1.0)).predict_proba_radiant({}) == HIGH

    def test_the_version_is_the_wrapped_model_not_the_wrapper(self) -> None:
        """The dashboard groups by this string. A wrapper name here would split one model's
        history in two the day the guard was added."""
        assert _Bounded(Certain(0.5)).version == "certain-1.0"

    def test_the_served_predictor_is_bounded(self) -> None:
        """The guard belongs to serving, not to the placeholder: phase 4 swaps the booster
        in, and a rule written into the baseline would have left with it."""
        reset_predictor()
        try:
            predictor = get_predictor()
            # A gold lead far past anything a real match produces.
            extreme = predictor.predict_proba_radiant({"gold_adv": 200_000.0, "minute": 60.0})
        finally:
            reset_predictor()

        assert extreme == HIGH

    def test_the_baseline_itself_still_saturates(self) -> None:
        """Kept honest on purpose: the baseline is spec section 7.3's benchmark and must not
        quietly become a different model because serving grew a guard."""
        raw = BaselinePredictor().predict_proba_radiant({"gold_adv": 200_000.0, "minute": 60.0})

        assert raw > HIGH
