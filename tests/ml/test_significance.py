"""A gate verdict has to be about the model, not about the holdout it was measured on.

The case this exists for is real and recent. `lgbm-20260901-073308` failed the 20-24 bucket
by 0.0012; the next model - same code, 557 more maps - won that bucket. Both verdicts were
reported as facts about the model. Neither was.
"""

import random

from app.ml.evaluate import evaluate
from app.ml.significance import BETTER, TIED, WORSE, paired_differences

MATCHES = 80
ROWS_PER_MATCH = 30


def synthetic(
    edge: float, seed: int = 7
) -> tuple[list[int], list[int], list[bool], list[float], list[float]]:
    """A holdout shaped like ours: many correlated rows per match, one outcome each.

    The candidate's advantage is a property **of the match**, not of the row, and that is the
    whole point of the fixture. A model that reads one game better reads most of its minutes
    better, so the per-row differences inside a match move together. Sprinkling independent
    per-row noise instead would make thirty rows behave like thirty matches - which is exactly
    the error the bootstrap is grouped by match to avoid, and a fixture with that shape cannot
    demonstrate the difference.

    `edge` is the mean advantage in probability space; a bigger edge is a better candidate.
    """
    rng = random.Random(seed)
    match_ids: list[int] = []
    minutes: list[int] = []
    truth: list[bool] = []
    candidate: list[float] = []
    baseline: list[float] = []

    for match_id in range(MATCHES):
        radiant_win = rng.random() < 0.5
        # How much better the candidate happens to be on this particular game.
        match_edge = edge + rng.gauss(0.0, 0.06)
        for row in range(ROWS_PER_MATCH):
            match_ids.append(match_id)
            minutes.append(row % 30)
            truth.append(radiant_win)
            base = 0.5 + (0.2 if radiant_win else -0.2) + rng.gauss(0.0, 0.05)
            base = min(max(base, 0.05), 0.95)
            baseline.append(base)
            shifted = base + (match_edge if radiant_win else -match_edge)
            candidate.append(min(max(shifted, 0.02), 0.98))
    return match_ids, minutes, truth, candidate, baseline


class TestPairedDifferences:
    def test_a_clearly_better_model_reads_as_better(self) -> None:
        match_ids, minutes, truth, probs, baseline = synthetic(edge=0.15)

        results = paired_differences(match_ids, minutes, truth, probs, baseline, "b")

        assert results, "no buckets compared"
        assert all(d.verdict == BETTER for d in results), [d.describe() for d in results]

    def test_a_clearly_worse_model_reads_as_worse(self) -> None:
        match_ids, minutes, truth, probs, baseline = synthetic(edge=-0.15)

        results = paired_differences(match_ids, minutes, truth, probs, baseline, "b")

        assert all(d.verdict == WORSE for d in results), [d.describe() for d in results]

    def test_two_models_of_the_same_quality_read_as_tied(self) -> None:
        """The regression: this is what 0.0012 on one bucket actually was."""
        match_ids, minutes, truth, probs, baseline = synthetic(edge=0.0)

        results = paired_differences(match_ids, minutes, truth, probs, baseline, "b")

        assert any(d.verdict == TIED for d in results), [d.describe() for d in results]

    def test_the_interval_brackets_the_observed_difference(self) -> None:
        match_ids, minutes, truth, probs, baseline = synthetic(edge=0.05)

        for difference in paired_differences(match_ids, minutes, truth, probs, baseline, "b"):
            assert difference.low <= difference.observed <= difference.high
            assert difference.margin_of_error > 0.0

    def test_resampling_by_row_would_report_a_narrower_interval(self) -> None:
        """Why the unit is the match (invariant 3, section 5.1).

        Thirty snapshots of one game are one observation. Handing each row its own id is the
        mistake this guards: the interval collapses, and a difference that means nothing
        starts reading as decisive.
        """
        match_ids, minutes, truth, probs, baseline = synthetic(edge=0.0)
        row_ids = list(range(len(truth)))

        by_match = paired_differences(match_ids, minutes, truth, probs, baseline, "b")
        by_row = paired_differences(row_ids, minutes, truth, probs, baseline, "b")

        matched = {d.bucket: d for d in by_match}
        for rowwise in by_row:
            assert rowwise.margin_of_error < matched[rowwise.bucket].margin_of_error

    def test_the_same_input_gives_the_same_verdict_twice(self) -> None:
        """A gate that answers differently on re-run is worse than no gate."""
        match_ids, minutes, truth, probs, baseline = synthetic(edge=0.01)

        first = paired_differences(match_ids, minutes, truth, probs, baseline, "b")
        second = paired_differences(match_ids, minutes, truth, probs, baseline, "b")

        assert [(d.bucket, d.low, d.high) for d in first] == [
            (d.bucket, d.low, d.high) for d in second
        ]

    def test_more_matches_narrow_the_interval(self) -> None:
        """The property that makes this better than a constant: it tightens as evidence grows."""
        match_ids, minutes, truth, probs, baseline = synthetic(edge=0.0)
        keep = MATCHES // 4
        small = [i for i, m in enumerate(match_ids) if m < keep]

        wide = paired_differences(
            [match_ids[i] for i in small],
            [minutes[i] for i in small],
            [truth[i] for i in small],
            [probs[i] for i in small],
            [baseline[i] for i in small],
            "b",
        )
        narrow = paired_differences(match_ids, minutes, truth, probs, baseline, "b")

        wide_by_bucket = {d.bucket: d for d in wide}
        for full in narrow:
            assert full.margin_of_error < wide_by_bucket[full.bucket].margin_of_error


class TestGateUsesIt:
    def test_a_difference_inside_the_noise_is_a_tie_not_a_failure(self) -> None:
        match_ids, minutes, truth, probs, baseline = synthetic(edge=0.0)

        result = evaluate(truth, probs, minutes, {"b": baseline}, match_ids)

        assert result.ties, "a coin-flip difference has to be reported as undecided"
        assert not any(tie in result.failures for tie in result.ties)

    def test_a_tie_is_visible_in_the_report_even_when_the_gate_passes(self) -> None:
        """'Passed' and 'passed on ties' are different facts and must not print the same."""
        from app.ml.evaluate import format_report

        match_ids, minutes, truth, probs, baseline = synthetic(edge=0.0)

        result = evaluate(truth, probs, minutes, {"b": baseline}, match_ids)
        report = format_report(result)

        if result.passes_gate and result.ties:
            assert "within the noise" in report

    def test_a_real_regression_still_fails(self) -> None:
        """The gate must not have been quietly turned off."""
        match_ids, minutes, truth, probs, baseline = synthetic(edge=-0.15)

        result = evaluate(truth, probs, minutes, {"b": baseline}, match_ids)

        assert not result.passes_gate
        assert result.failures
