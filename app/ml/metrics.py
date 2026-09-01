"""Validation metrics (spec section 7.2).

Log loss and Brier, never accuracy - a model that is right 70% of the time while being
wildly overconfident is worse than one that is right 68% of the time and honest about it,
and accuracy cannot tell them apart.

**Everything is reported per minute bucket.** The averaged figure lies: by minute 40 the
outcome is close to decided, so a model that is useless early and merely adequate late
still posts a respectable mean. Section 7.2 names the buckets and this module fixes them.

Pure Python on purpose. The test suite runs in the API image, which does not carry the `ml`
extra, and a metric that cannot be tested where the tests run is a metric nobody checks.
"""

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass

#: Clamp before the logarithm. A single confidently-wrong row would otherwise send log loss
#: to infinity and decide any comparison on its own.
_EPS = 1e-15


@dataclass(frozen=True)
class MinuteBucket:
    label: str
    start: int
    end: int | None  # exclusive; None means open-ended


#: Spec section 7.2: 5 / 10 / 15 / 20 / 25 / 30+.
MINUTE_BUCKETS: tuple[MinuteBucket, ...] = (
    MinuteBucket("0-4", 0, 5),
    MinuteBucket("5-9", 5, 10),
    MinuteBucket("10-14", 10, 15),
    MinuteBucket("15-19", 15, 20),
    MinuteBucket("20-24", 20, 25),
    MinuteBucket("25-29", 25, 30),
    MinuteBucket("30+", 30, None),
)


@dataclass(frozen=True)
class ReliabilityPoint:
    """One bin of a reliability diagram: what was promised against what happened."""

    predicted: float
    observed: float
    count: int


def _checked(y_true: Sequence[bool], y_prob: Sequence[float]) -> None:
    if len(y_true) != len(y_prob):
        raise ValueError(f"length mismatch: {len(y_true)} labels, {len(y_prob)} predictions")
    if not y_true:
        # Returning 0.0 here would read as a perfect score on an empty slice, which is how
        # an empty minute bucket silently becomes the best one in the table.
        raise ValueError("no rows to score")


def row_log_loss(actual: bool, p: float) -> float:
    """One row's negative log likelihood.

    Exported so the significance tests can weigh individual rows against each other with the
    same clamp `log_loss` uses. Two clamps would be two metrics wearing one name.
    """
    clipped = min(max(float(p), _EPS), 1.0 - _EPS)
    return -math.log(clipped) if actual else -math.log(1.0 - clipped)


def log_loss(y_true: Sequence[bool], y_prob: Sequence[float]) -> float:
    """Mean negative log likelihood. Lower is better; 0.693 is a coin flip."""
    _checked(y_true, y_prob)
    return sum(row_log_loss(actual, p) for actual, p in zip(y_true, y_prob, strict=True)) / len(
        y_true
    )


def brier(y_true: Sequence[bool], y_prob: Sequence[float]) -> float:
    """Mean squared error on the probability. Lower is better; 0.25 is a coin flip."""
    _checked(y_true, y_prob)
    return sum(
        (float(p) - (1.0 if actual else 0.0)) ** 2 for actual, p in zip(y_true, y_prob, strict=True)
    ) / len(y_true)


def accuracy(y_true: Sequence[bool], y_prob: Sequence[float]) -> float:
    """Reported alongside the others because people ask for it, never optimised for."""
    _checked(y_true, y_prob)
    return sum(
        1 for actual, p in zip(y_true, y_prob, strict=True) if (p >= 0.5) is bool(actual)
    ) / len(y_true)


def bucket_of(minute: int) -> str:
    for bucket in MINUTE_BUCKETS:
        if minute >= bucket.start and (bucket.end is None or minute < bucket.end):
            return bucket.label
    raise ValueError(f"minute {minute} falls outside every bucket")


def by_bucket(
    minutes: Sequence[int],
    y_true: Sequence[bool],
    y_prob: Sequence[float],
    metric: Callable[[Sequence[bool], Sequence[float]], float],
) -> dict[str, float]:
    """Apply a metric within each minute bucket.

    Buckets with no rows are absent from the result rather than present as zero - a missing
    bucket is a gap in the evaluation, and zero would read as a perfect score.
    """
    if len(minutes) != len(y_true):
        raise ValueError("minutes and labels must be the same length")

    grouped: dict[str, tuple[list[bool], list[float]]] = {}
    for minute, actual, p in zip(minutes, y_true, y_prob, strict=True):
        labels, probs = grouped.setdefault(bucket_of(minute), ([], []))
        labels.append(bool(actual))
        probs.append(float(p))

    ordered = [b.label for b in MINUTE_BUCKETS if b.label in grouped]
    return {label: metric(*grouped[label]) for label in ordered}


def reliability_curve(
    y_true: Sequence[bool], y_prob: Sequence[float], bins: int = 10
) -> list[ReliabilityPoint]:
    """Reliability diagram: mean predicted probability against observed frequency per bin.

    This is what the public accuracy dashboard draws (F6). Empty bins are dropped rather
    than plotted at zero, which would draw a line through territory nothing was predicted in.
    """
    _checked(y_true, y_prob)
    if bins < 1:
        raise ValueError("bins must be positive")

    buckets: list[tuple[list[bool], list[float]]] = [([], []) for _ in range(bins)]
    for actual, p in zip(y_true, y_prob, strict=True):
        index = min(int(float(p) * bins), bins - 1)
        buckets[index][0].append(bool(actual))
        buckets[index][1].append(float(p))

    return [
        ReliabilityPoint(
            predicted=sum(probs) / len(probs),
            observed=sum(1 for a in labels if a) / len(labels),
            count=len(labels),
        )
        for labels, probs in buckets
        if labels
    ]


def expected_calibration_error(
    y_true: Sequence[bool], y_prob: Sequence[float], bins: int = 10
) -> float:
    """Weighted mean gap between promised and observed frequency.

    The number the accuracy dashboard leads with, and the one section 11 wants an alert on:
    a model whose probabilities drift stops being useful long before its log loss looks bad.
    """
    curve = reliability_curve(y_true, y_prob, bins)
    total = sum(point.count for point in curve)
    return sum(abs(point.observed - point.predicted) * point.count for point in curve) / total
