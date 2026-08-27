"""The baselines a trained model must beat (spec section 7.3).

Section 7.3 is blunt about why these exist: a logistic regression on gold advantage and
minute is a surprisingly strong predictor, and if boosting cannot clearly beat it there is a
bug somewhere. That only holds if the baseline is fitted honestly - a crippled baseline
turns the gate into a formality and hides exactly the bug it was meant to catch.

Pure Python, like `metrics`. These run in the evaluation gate, which runs wherever the tests
run, and the API image does not carry the `ml` extra.
"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, Self

#: Keeps a degenerate sample (every game won by one side) from producing a certainty. One
#: surprise against p=1.0 scores infinity and decides any comparison on its own.
_LAPLACE = 1.0


def _sigmoid(z: float) -> float:
    return 1.0 / (1.0 + math.exp(-max(min(z, 35.0), -35.0)))


class Baseline(Protocol):
    # A read-only property rather than a plain attribute: the implementations are frozen
    # dataclasses, and a settable protocol member would exclude every one of them.
    @property
    def name(self) -> str: ...

    def predict(self, rows: Sequence[Mapping[str, float]]) -> list[float]: ...


@dataclass(frozen=True)
class ConstantBaseline:
    """Baseline 1: a coin flip. Scores ln(2) = 0.693; anything worse is actively harmful."""

    name: str = "constant-0.5"

    def predict(self, rows: Sequence[Mapping[str, float]]) -> list[float]:
        return [0.5] * len(rows)


@dataclass(frozen=True)
class RadiantBiasBaseline:
    """Baseline 2: the historical Radiant win rate, ignoring the game entirely.

    Worth keeping honest: on the current sample Radiant wins 50.9% of maps, so this is
    barely distinguishable from the coin flip. On a wider sample the spec expects 52-55%.
    """

    p: float
    name: str = "radiant-bias"

    @classmethod
    def fit(cls, labels: Sequence[bool]) -> Self:
        wins = sum(1 for label in labels if label)
        # Laplace-smoothed so an all-wins sample cannot yield p = 1.0.
        return cls(p=(wins + _LAPLACE) / (len(labels) + 2 * _LAPLACE))

    def predict(self, rows: Sequence[Mapping[str, float]]) -> list[float]:
        return [self.p] * len(rows)


@dataclass(frozen=True)
class GoldMinuteLogistic:
    """Baseline 3: logistic regression on gold advantage and minute.

    The features are `gold_adv`, `minute` and **their interaction**. The spec names the first
    two; the third is here because the same 5k lead means something very different at minute
    10 and at minute 40, and a baseline without it is weak for a reason that has nothing to
    do with the model being good.

    Fitted by gradient descent on standardised features - deterministic, dependency-free, and
    entirely adequate for three coefficients.
    """

    weights: tuple[float, ...]
    bias: float
    means: tuple[float, ...]
    scales: tuple[float, ...]
    name: str = "logistic-gold-minute"

    @staticmethod
    def _design(row: Mapping[str, float]) -> tuple[float, float, float]:
        gold = float(row.get("gold_adv", 0.0))
        minute = float(row.get("minute", 0.0))
        return gold, minute, gold * minute

    @classmethod
    def fit(
        cls,
        rows: Sequence[Mapping[str, float]],
        labels: Sequence[bool],
        iterations: int = 2000,
        learning_rate: float = 0.5,
    ) -> Self:
        if len(rows) != len(labels):
            raise ValueError("rows and labels must be the same length")
        if not rows:
            raise ValueError("nothing to fit")

        design = [cls._design(row) for row in rows]
        n, k = len(design), 3

        means = tuple(sum(point[j] for point in design) / n for j in range(k))
        scales = tuple(
            max(math.sqrt(sum((point[j] - means[j]) ** 2 for point in design) / n), 1e-9)
            for j in range(k)
        )
        scaled = [tuple((point[j] - means[j]) / scales[j] for j in range(k)) for point in design]
        targets = [1.0 if label else 0.0 for label in labels]

        weights = [0.0] * k
        bias = 0.0
        for _ in range(iterations):
            grad_w = [0.0] * k
            grad_b = 0.0
            for point, target in zip(scaled, targets, strict=True):
                error = _sigmoid(sum(w * x for w, x in zip(weights, point, strict=True)) + bias)
                error -= target
                grad_b += error
                for j in range(k):
                    grad_w[j] += error * point[j]
            bias -= learning_rate * grad_b / n
            for j in range(k):
                weights[j] -= learning_rate * grad_w[j] / n

        return cls(weights=tuple(weights), bias=bias, means=means, scales=scales)

    def predict(self, rows: Sequence[Mapping[str, float]]) -> list[float]:
        out = []
        for row in rows:
            point = self._design(row)
            z = self.bias + sum(
                w * (x - m) / s
                for w, x, m, s in zip(self.weights, point, self.means, self.scales, strict=True)
            )
            out.append(_sigmoid(z))
        return out


def fit_baselines(rows: Sequence[Mapping[str, float]], labels: Sequence[bool]) -> list[Baseline]:
    """Every baseline from section 7.3 that applies to the live model.

    The fourth - the pre-match Elo favourite - belongs to the pre-match model and is not
    comparable here: it makes one prediction per map, while these make one per minute.
    """
    return [
        ConstantBaseline(),
        RadiantBiasBaseline.fit(labels),
        GoldMinuteLogistic.fit(rows, labels),
    ]
