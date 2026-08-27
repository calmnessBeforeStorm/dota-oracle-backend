"""Probability calibration (spec sections 7.2, 8.1/F6)."""

import math
import random
from itertools import pairwise

import pytest

from app.ml.calibration import IdentityCalibrator, PlattCalibrator
from app.ml.metrics import expected_calibration_error, log_loss


def overconfident(n: int = 3000, seed: int = 11) -> tuple[list[float], list[bool]]:
    """A model whose ranking is right but whose probabilities are pushed to the extremes."""
    rng = random.Random(seed)
    raw: list[float] = []
    labels: list[bool] = []
    for _ in range(n):
        true_p = rng.uniform(0.05, 0.95)
        labels.append(rng.random() < true_p)
        # Same ordering, exaggerated: log-odds doubled.
        raw.append(1.0 / (1.0 + math.exp(-2.0 * math.log(true_p / (1 - true_p)))))
    return raw, labels


class TestPlatt:
    def test_reduces_calibration_error(self) -> None:
        raw, labels = overconfident()
        calibrated = PlattCalibrator.fit(raw, labels).apply(raw)

        assert expected_calibration_error(labels, calibrated) < expected_calibration_error(
            labels, raw
        )

    def test_improves_log_loss_on_an_overconfident_model(self) -> None:
        raw, labels = overconfident()
        calibrated = PlattCalibrator.fit(raw, labels).apply(raw)

        assert log_loss(labels, calibrated) < log_loss(labels, raw)

    def test_preserves_the_ranking(self) -> None:
        """Calibration must not reorder anything - it only rescales confidence."""
        raw, labels = overconfident(500)
        calibrator = PlattCalibrator.fit(raw, labels)
        calibrated = calibrator.apply(raw)

        pairs = sorted(zip(raw, calibrated, strict=True))
        assert all(earlier[1] <= later[1] for earlier, later in pairwise(pairs))

    def test_output_stays_a_probability(self) -> None:
        raw, labels = overconfident(500)
        calibrated = PlattCalibrator.fit(raw, labels).apply([0.0, 1.0, 0.5])
        assert all(0.0 < p < 1.0 for p in calibrated)

    def test_an_already_calibrated_model_is_left_roughly_alone(self) -> None:
        rng = random.Random(5)
        raw = [rng.uniform(0.05, 0.95) for _ in range(3000)]
        labels = [rng.random() < p for p in raw]

        calibrated = PlattCalibrator.fit(raw, labels).apply(raw)

        assert max(abs(a - b) for a, b in zip(raw, calibrated, strict=True)) < 0.1

    def test_fitting_on_nothing_is_refused(self) -> None:
        with pytest.raises(ValueError):
            PlattCalibrator.fit([], [])

    def test_length_mismatch_is_refused(self) -> None:
        with pytest.raises(ValueError):
            PlattCalibrator.fit([0.5], [True, False])


class TestIdentity:
    def test_returns_its_input(self) -> None:
        assert IdentityCalibrator().apply([0.1, 0.9]) == [0.1, 0.9]
