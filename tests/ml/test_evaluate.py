"""The gate a model has to pass before it may be served (spec sections 7.2, 7.3).

The rule is deliberately strict: better *on average* is not enough, because the average is
dominated by late minutes where the outcome is nearly decided. A model that is worse than a
two-feature logistic regression at minute 10 is not useful, however good its mean looks.
"""

import pytest

from app.ml.evaluate import evaluate


def rows(n: int = 600) -> tuple[list[int], list[bool]]:
    minutes = [m % 40 for m in range(n)]
    truth = [m % 40 < 20 for m in range(n)]
    return minutes, truth


class TestEvaluation:
    def test_reports_metrics_overall_and_per_bucket(self) -> None:
        minutes, truth = rows()
        probs = [0.9 if t else 0.1 for t in truth]

        result = evaluate(truth, probs, minutes, baselines={})

        assert result.log_loss < 0.2
        assert 0.0 <= result.ece <= 1.0
        assert set(result.log_loss_by_minute) <= {
            "0-4",
            "5-9",
            "10-14",
            "15-19",
            "20-24",
            "25-29",
            "30+",
        }
        assert result.sample_size == len(truth)

    def test_a_model_better_everywhere_passes(self) -> None:
        minutes, truth = rows()
        good = [0.95 if t else 0.05 for t in truth]
        weak = [0.55 if t else 0.45 for t in truth]

        result = evaluate(truth, good, minutes, baselines={"weak": weak})

        assert result.passes_gate
        assert result.failures == []

    def test_a_model_worse_in_one_bucket_fails(self) -> None:
        """Even when it wins on the average - which is the whole point of the rule."""
        minutes, truth = rows()
        baseline = [0.9 if t else 0.1 for t in truth]
        # Excellent everywhere except the first bucket, where it is a coin flip.
        candidate = [
            0.5 if minute < 5 else (0.99 if t else 0.01)
            for minute, t in zip(minutes, truth, strict=True)
        ]

        result = evaluate(truth, candidate, minutes, baselines={"strong": baseline})

        assert result.log_loss < evaluate(truth, baseline, minutes, baselines={}).log_loss
        assert not result.passes_gate
        assert any("0-4" in failure and "strong" in failure for failure in result.failures)

    def test_every_baseline_is_checked_not_just_the_best(self) -> None:
        minutes, truth = rows()
        candidate = [0.7 if t else 0.3 for t in truth]
        beatable = [0.5] * len(truth)
        unbeatable = [0.999 if t else 0.001 for t in truth]

        result = evaluate(
            truth, candidate, minutes, baselines={"easy": beatable, "hard": unbeatable}
        )

        assert not result.passes_gate
        assert all("hard" in failure for failure in result.failures)

    def test_a_bucket_the_baseline_lacks_is_not_silently_skipped(self) -> None:
        """Comparing against a baseline that has no rows in a bucket would otherwise count
        as a pass for that bucket."""
        minutes = [1, 1, 35, 35]
        truth = [True, False, True, False]
        candidate = [0.6, 0.4, 0.6, 0.4]
        baseline = [0.5, 0.5, 0.5, 0.5]

        result = evaluate(truth, candidate, minutes, baselines={"flat": baseline})

        assert set(result.log_loss_by_minute) == {"0-4", "30+"}
        assert result.passes_gate

    def test_length_mismatch_is_refused(self) -> None:
        with pytest.raises(ValueError):
            evaluate([True, False], [0.5], [1, 2], baselines={})

    def test_as_log_fields_is_flat_enough_to_log(self) -> None:
        minutes, truth = rows(100)
        result = evaluate(truth, [0.5] * 100, minutes, baselines={})
        fields = result.as_log_fields()
        assert isinstance(fields["log_loss"], float)
        assert isinstance(fields["passes_gate"], bool)
