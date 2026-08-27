"""Validation metrics (spec section 7.2).

Log loss and Brier, never accuracy, and always **per minute bucket**: the averaged figure
lies, because predicting minute 40 is close to trivial and drags the mean down until any
model looks good.
"""

import math

import pytest

from app.ml.metrics import (
    MINUTE_BUCKETS,
    brier,
    bucket_of,
    by_bucket,
    expected_calibration_error,
    log_loss,
    reliability_curve,
)


class TestLogLoss:
    def test_perfect_prediction_scores_zero(self) -> None:
        assert log_loss([True, False], [1.0, 0.0]) == pytest.approx(0.0, abs=1e-9)

    def test_coin_flip_scores_ln_two(self) -> None:
        assert log_loss([True, False, True], [0.5, 0.5, 0.5]) == pytest.approx(math.log(2))

    def test_confident_and_wrong_is_finite(self) -> None:
        """A single confidently wrong prediction must not make the metric infinite -
        one row would then decide the whole comparison."""
        value = log_loss([True], [0.0])
        assert math.isfinite(value)
        assert value > 10

    def test_rewards_the_better_of_two_models(self) -> None:
        truth = [True, True, False, False]
        good = log_loss(truth, [0.9, 0.8, 0.2, 0.1])
        bad = log_loss(truth, [0.6, 0.55, 0.45, 0.4])
        assert good < bad

    def test_length_mismatch_is_an_error(self) -> None:
        with pytest.raises(ValueError):
            log_loss([True, False], [0.5])

    def test_empty_input_is_an_error(self) -> None:
        """Silently returning 0.0 would read as a perfect score on an empty bucket."""
        with pytest.raises(ValueError):
            log_loss([], [])


class TestBrier:
    def test_perfect_prediction_scores_zero(self) -> None:
        assert brier([True, False], [1.0, 0.0]) == pytest.approx(0.0)

    def test_coin_flip_scores_a_quarter(self) -> None:
        assert brier([True, False], [0.5, 0.5]) == pytest.approx(0.25)

    def test_worst_case_scores_one(self) -> None:
        assert brier([True, False], [0.0, 1.0]) == pytest.approx(1.0)


class TestMinuteBuckets:
    def test_buckets_cover_every_minute_exactly_once(self) -> None:
        seen = [bucket_of(m) for m in range(0, 120)]
        assert all(s in {b.label for b in MINUTE_BUCKETS} for s in seen)

    def test_boundaries_land_where_the_spec_says(self) -> None:
        """Spec section 7.2 names 5 / 10 / 15 / 20 / 25 / 30+."""
        assert bucket_of(0) == "0-4"
        assert bucket_of(4) == "0-4"
        assert bucket_of(5) == "5-9"
        assert bucket_of(29) == "25-29"
        assert bucket_of(30) == "30+"
        assert bucket_of(75) == "30+"

    def test_by_bucket_splits_the_metric(self) -> None:
        minutes = [1, 2, 31, 32]
        truth = [True, True, True, False]
        probs = [0.5, 0.5, 1.0, 0.0]
        result = by_bucket(minutes, truth, probs, log_loss)
        assert set(result) == {"0-4", "30+"}
        assert result["0-4"] == pytest.approx(math.log(2))
        assert result["30+"] == pytest.approx(0.0, abs=1e-9)

    def test_empty_buckets_are_absent_not_zero(self) -> None:
        """A bucket with no rows must not appear as a perfect score."""
        result = by_bucket([1], [True], [0.5], log_loss)
        assert set(result) == {"0-4"}


class TestCalibration:
    def test_perfectly_calibrated_predictions_have_no_error(self) -> None:
        # Ten rows at p=0.5 of which exactly half are wins.
        truth = [True, False] * 20
        probs = [0.5] * 40
        assert expected_calibration_error(truth, probs, bins=10) == pytest.approx(0.0, abs=1e-9)

    def test_systematic_overconfidence_is_caught(self) -> None:
        """Always says 90%, is right half the time - ECE must be near 0.4."""
        truth = [True, False] * 20
        probs = [0.9] * 40
        assert expected_calibration_error(truth, probs, bins=10) == pytest.approx(0.4, abs=0.01)

    def test_reliability_curve_reports_count_per_bin(self) -> None:
        truth = [True] * 10 + [False] * 10
        probs = [1.0] * 10 + [0.0] * 10
        curve = reliability_curve(truth, probs, bins=10)

        assert sum(point.count for point in curve) == 20
        assert len(curve) == 2  # empty bins are dropped, not plotted at zero
        for point in curve:
            assert point.observed == pytest.approx(point.predicted)
            assert point.count == 10

    def test_reliability_curve_shows_where_a_model_lies(self) -> None:
        """Promises 80%, delivers 50%. The gap is what the dashboard draws."""
        truth = [True, False] * 10
        curve = reliability_curve(truth, [0.8] * 20, bins=10)

        assert len(curve) == 1
        assert curve[0].predicted == pytest.approx(0.8)
        assert curve[0].observed == pytest.approx(0.5)
