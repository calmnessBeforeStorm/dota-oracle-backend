"""How much of a gate verdict is evidence, and how much is the holdout it was measured on.

The gate compares the candidate's log loss against a baseline's, bucket by bucket, and fails
the model if it loses anywhere (`evaluate`). That rule has no notion of how big a difference
has to be before it means anything, and the difference is an estimate like any other. Measured
on our own runs: `lgbm-20260901-073308` failed the 20-24 bucket at 0.4885 against 0.4873 - a
gap of 0.0012 - and the very next model, same code and 557 more maps, won that bucket. Neither
verdict described the model. Both described the sample.

The drift alert already refuses to work this way: its threshold is derived from the size of
the window it is looking at, because "a threshold that is right today is wrong after a month
of data". This is the same argument applied to the gate, with one difference that makes it
easier - the comparison here is **paired**. Both models score the same rows, so the noise in
"which matches ended up in the holdout" cancels, and what is left can be measured directly by
resampling those matches.

**Resampled by match, never by row** (invariant 3, spec section 5.1). Forty snapshots of one
game are forty views of one outcome; bootstrapping rows would treat them as forty independent
matches and report an interval several times too narrow - which is precisely the error that
makes 0.0012 look like a verdict.

**Seeded.** A gate that answers differently on re-run is worse than no gate, so the resampling
is deterministic and the seed is part of the rule rather than a convenience.
"""

import random
from collections.abc import Sequence
from dataclasses import dataclass

from app.ml.metrics import bucket_of, row_log_loss

#: Enough for a stable 10th/90th percentile without making the gate the slow part of a run.
#: Measured on the 1226-match holdout: 500 resamples move an interval endpoint by less than
#: 0.0002 between seeds, which is an order of magnitude below the differences being judged.
RESAMPLES = 500

#: Part of the rule, not a default. See the module docstring.
SEED = 20260901

#: The interval reported and used. One-sided 10%/90% rather than a 95% interval: the question
#: is "which side is this on", not "publish a confidence interval", and p90 is the convention
#: the drift alert already uses.
LOW_PERCENTILE = 0.10
HIGH_PERCENTILE = 0.90

BETTER = "better"
TIED = "tied"
WORSE = "worse"


@dataclass(frozen=True)
class PairedDifference:
    """One bucket, one baseline: how much worse the candidate is, and how sure we are.

    `observed` is candidate minus baseline, so **negative means the candidate is better**.
    """

    bucket: str
    baseline: str
    observed: float
    low: float
    high: float
    matches: int
    rows: int

    @property
    def verdict(self) -> str:
        if self.high < 0.0:
            return BETTER
        if self.low > 0.0:
            return WORSE
        return TIED

    @property
    def margin_of_error(self) -> float:
        """Half the interval - the number to compare a claimed improvement against."""
        return (self.high - self.low) / 2.0

    def describe(self) -> str:
        return (
            f"{self.bucket} vs {self.baseline}: {self.observed:+.4f} "
            f"(bootstrap {self.low:+.4f} .. {self.high:+.4f}, {self.matches} matches)"
        )


def _percentile(sorted_values: list[float], fraction: float) -> float:
    if not sorted_values:
        raise ValueError("no values to take a percentile of")
    index = min(int(fraction * len(sorted_values)), len(sorted_values) - 1)
    return sorted_values[index]


def paired_differences(
    match_ids: Sequence[int],
    minutes: Sequence[int],
    y_true: Sequence[bool],
    y_prob: Sequence[float],
    baseline_prob: Sequence[float],
    baseline_name: str,
    *,
    resamples: int = RESAMPLES,
    seed: int = SEED,
) -> list[PairedDifference]:
    """Per bucket, the candidate's log-loss disadvantage and a bootstrap interval for it."""
    if not (len(match_ids) == len(minutes) == len(y_true) == len(y_prob) == len(baseline_prob)):
        raise ValueError("all inputs must be the same length")

    # Per bucket, per match: how much log loss the candidate spends over the baseline, and on
    # how many rows. A match is the unit that gets resampled, so it is the unit accumulated.
    per_match: dict[str, dict[int, list[float]]] = {}
    for match_id, minute, actual, p, q in zip(
        match_ids, minutes, y_true, y_prob, baseline_prob, strict=True
    ):
        in_bucket = per_match.setdefault(bucket_of(int(minute)), {})
        totals = in_bucket.setdefault(int(match_id), [0.0, 0.0])
        totals[0] += row_log_loss(bool(actual), float(p)) - row_log_loss(bool(actual), float(q))
        totals[1] += 1.0

    results: list[PairedDifference] = []
    for bucket, matches in per_match.items():
        rng = random.Random(f"{seed}:{baseline_name}:{bucket}")
        sums = [totals[0] for totals in matches.values()]
        counts = [totals[1] for totals in matches.values()]
        n = len(sums)

        observed = sum(sums) / sum(counts)

        draws: list[float] = []
        for _ in range(resamples):
            picked = rng.choices(range(n), k=n)
            total = sum(sums[i] for i in picked)
            rows = sum(counts[i] for i in picked)
            draws.append(total / rows)
        draws.sort()

        results.append(
            PairedDifference(
                bucket=bucket,
                baseline=baseline_name,
                observed=observed,
                low=_percentile(draws, LOW_PERCENTILE),
                high=_percentile(draws, HIGH_PERCENTILE),
                matches=n,
                rows=int(sum(counts)),
            )
        )
    return results
