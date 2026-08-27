"""Probability calibration (spec sections 7.2, 8.1/F6).

A boosted tree optimising log loss already produces something probability-shaped, but not
something you can put on a public dashboard: it tends to be over-confident at the extremes,
and F6 exists precisely to show that the numbers can be trusted.

**Platt scaling, not isotonic.** Isotonic regression is the stronger method and the usual
recommendation - on enough data. It is fitted on the validation slice, which currently holds
about seventy matches, and on that it would happily reproduce its own noise as a step
function. Platt has two parameters and cannot. Revisit when the validation slice is measured
in thousands of matches rather than tens.

Pure Python so the calibrator can be tested where the tests run.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Self

_EPS = 1e-12


def _sigmoid(z: float) -> float:
    return 1.0 / (1.0 + math.exp(-max(min(z, 35.0), -35.0)))


def _logit(p: float) -> float:
    clipped = min(max(p, _EPS), 1.0 - _EPS)
    return math.log(clipped / (1.0 - clipped))


@dataclass(frozen=True)
class PlattCalibrator:
    """Fits `p_calibrated = sigmoid(a * logit(p_raw) + b)`.

    Works on the log-odds rather than on the raw probability: a model that is merely
    over-confident needs `a < 1`, which is a straight line in log-odds space and a curve in
    probability space.
    """

    a: float
    b: float

    @classmethod
    def fit(
        cls,
        raw: Sequence[float],
        labels: Sequence[bool],
        iterations: int = 2000,
        learning_rate: float = 0.5,
    ) -> Self:
        if len(raw) != len(labels):
            raise ValueError("raw scores and labels must be the same length")
        if not raw:
            raise ValueError("nothing to calibrate on")

        scores = [_logit(float(p)) for p in raw]
        targets = [1.0 if label else 0.0 for label in labels]
        n = len(scores)

        mean = sum(scores) / n
        scale = max(math.sqrt(sum((s - mean) ** 2 for s in scores) / n), 1e-9)
        standardised = [(s - mean) / scale for s in scores]

        weight, bias = 1.0, 0.0
        for _ in range(iterations):
            grad_w = grad_b = 0.0
            for x, target in zip(standardised, targets, strict=True):
                error = _sigmoid(weight * x + bias) - target
                grad_w += error * x
                grad_b += error
            weight -= learning_rate * grad_w / n
            bias -= learning_rate * grad_b / n

        # Fold the standardisation back into the two coefficients so `apply` stays trivial.
        a = weight / scale
        return cls(a=a, b=bias - a * mean)

    def apply(self, raw: Sequence[float]) -> list[float]:
        return [_sigmoid(self.a * _logit(float(p)) + self.b) for p in raw]


@dataclass(frozen=True)
class IdentityCalibrator:
    """Used when there is nothing to fit on. Named rather than implied, so a run that
    skipped calibration says so in its model card."""

    def apply(self, raw: Sequence[float]) -> list[float]:
        return [float(p) for p in raw]
