"""Scoring a candidate model, and the gate that decides whether it may be served.

Spec section 7.3 says the model must beat the baselines. Section 7.2 says every metric is
reported per minute bucket because the average lies. Put together, those give the rule this
module enforces:

    a candidate passes only if it beats **every** baseline in **every** minute bucket.

That is stricter than "better on average", and deliberately so. The average is dominated by
late minutes, where a 15k gold lead makes the answer nearly free; a model that is worse than
a two-feature logistic regression at minute 10 has no value, however good its mean looks.

The gate does not delete anything or refuse to write an artifact - it only decides whether
the model is allowed to become the active one. A model that fails is still worth keeping and
looking at.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.ml.metrics import (
    ReliabilityPoint,
    accuracy,
    brier,
    by_bucket,
    expected_calibration_error,
    log_loss,
    reliability_curve,
)


@dataclass(frozen=True)
class Evaluation:
    sample_size: int
    log_loss: float
    brier: float
    accuracy: float
    ece: float
    log_loss_by_minute: dict[str, float]
    brier_by_minute: dict[str, float]
    #: baseline name -> its log loss per bucket, for the report the human reads.
    baseline_log_loss_by_minute: dict[str, dict[str, float]] = field(default_factory=dict)
    #: One line per (bucket, baseline) the candidate failed to beat. Empty means it passed.
    failures: list[str] = field(default_factory=list)
    #: What the accuracy dashboard draws: promised probability against observed frequency.
    reliability: list[ReliabilityPoint] = field(default_factory=list)

    @property
    def passes_gate(self) -> bool:
        return not self.failures

    def as_log_fields(self) -> dict[str, Any]:
        return {
            "sample_size": self.sample_size,
            "log_loss": self.log_loss,
            "brier": self.brier,
            "accuracy": self.accuracy,
            "ece": self.ece,
            "passes_gate": self.passes_gate,
            "failures": len(self.failures),
        }


def evaluate(
    y_true: Sequence[bool],
    y_prob: Sequence[float],
    minutes: Sequence[int],
    baselines: Mapping[str, Sequence[float]],
) -> Evaluation:
    """Score a candidate and compare it to each baseline, bucket by bucket."""
    if not (len(y_true) == len(y_prob) == len(minutes)):
        raise ValueError(
            f"length mismatch: {len(y_true)} labels, {len(y_prob)} predictions, "
            f"{len(minutes)} minutes"
        )

    candidate_by_bucket = by_bucket(minutes, y_true, y_prob, log_loss)

    baseline_by_bucket: dict[str, dict[str, float]] = {}
    failures: list[str] = []
    for name, baseline_prob in baselines.items():
        if len(baseline_prob) != len(y_true):
            raise ValueError(
                f"baseline {name} has {len(baseline_prob)} rows, expected {len(y_true)}"
            )
        scored = by_bucket(minutes, y_true, baseline_prob, log_loss)
        baseline_by_bucket[name] = scored

        for bucket, candidate_score in candidate_by_bucket.items():
            # A bucket the baseline has no rows in cannot be compared. It cannot happen with
            # baselines scored on the same rows, and if it ever does, treating it as a pass
            # would hide the gap rather than report it.
            if bucket not in scored:
                failures.append(f"{bucket}: baseline {name} has no rows to compare against")
            elif candidate_score >= scored[bucket]:
                failures.append(
                    f"{bucket}: log loss {candidate_score:.4f} does not beat "
                    f"baseline {name} at {scored[bucket]:.4f}"
                )

    return Evaluation(
        sample_size=len(y_true),
        log_loss=log_loss(y_true, y_prob),
        brier=brier(y_true, y_prob),
        accuracy=accuracy(y_true, y_prob),
        ece=expected_calibration_error(y_true, y_prob),
        log_loss_by_minute=candidate_by_bucket,
        brier_by_minute=by_bucket(minutes, y_true, y_prob, brier),
        baseline_log_loss_by_minute=baseline_by_bucket,
        failures=failures,
        reliability=reliability_curve(y_true, y_prob),
    )


def format_report(result: Evaluation) -> str:
    """The table a human reads before deciding to trust a run.

    Per bucket, because that is the only form in which these numbers mean anything.
    """
    lines = [
        f"rows: {result.sample_size}   log loss: {result.log_loss:.4f}   "
        f"brier: {result.brier:.4f}   ECE: {result.ece:.4f}   acc: {result.accuracy:.3f}",
        "",
    ]

    names = list(result.baseline_log_loss_by_minute)
    header = f"{'bucket':>8}  {'model':>8}" + "".join(f"  {n[:14]:>14}" for n in names)
    lines.append(header)
    lines.append("-" * len(header))

    for bucket, score in result.log_loss_by_minute.items():
        row = f"{bucket:>8}  {score:>8.4f}"
        for name in names:
            baseline_score = result.baseline_log_loss_by_minute[name].get(bucket)
            row += f"  {baseline_score:>14.4f}" if baseline_score is not None else f"  {'-':>14}"
        lines.append(row)

    lines.append("")
    if result.passes_gate:
        lines.append("GATE: passed - beats every baseline in every minute bucket")
    else:
        lines.append(f"GATE: FAILED ({len(result.failures)} comparisons)")
        lines.extend(f"  {failure}" for failure in result.failures)

    # Calibration is what the public dashboard shows (F6), so it is worth seeing here too:
    # a model can win every bucket on log loss and still promise 80% where it delivers 60%.
    if result.reliability:
        lines.append("")
        lines.append(f"{'predicted':>10}  {'observed':>9}  {'rows':>7}")
        for point in result.reliability:
            lines.append(f"{point.predicted:>10.3f}  {point.observed:>9.3f}  {point.count:>7}")

    return "\n".join(lines)
