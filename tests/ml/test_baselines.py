"""The baselines a real model has to beat (spec section 7.3).

These are not decoration. Section 7.3 is explicit that if boosting cannot clearly beat a
logistic regression on two features, there is a bug somewhere - so the baseline has to be
fitted honestly, not crippled to make the gate easy to pass.
"""

import math
import random

import pytest

from app.ml.baselines import ConstantBaseline, GoldMinuteLogistic, RadiantBiasBaseline
from app.ml.metrics import log_loss


def synthetic(n: int = 4000, seed: int = 7) -> tuple[list[dict[str, float]], list[bool]]:
    """Gold leads decide games, and decide them harder the later it gets."""
    rng = random.Random(seed)
    rows: list[dict[str, float]] = []
    labels: list[bool] = []
    for _ in range(n):
        minute = rng.randint(0, 50)
        gold = rng.gauss(0, 6000)
        z = gold * (0.0002 + 0.00001 * minute)
        rows.append({"gold_adv": gold, "minute": float(minute)})
        labels.append(rng.random() < 1 / (1 + math.exp(-z)))
    return rows, labels


class TestConstant:
    def test_always_returns_a_half(self) -> None:
        model = ConstantBaseline()
        assert model.predict([{"gold_adv": 9999.0, "minute": 40.0}]) == [0.5]

    def test_scores_ln_two_on_a_balanced_sample(self) -> None:
        rows, labels = synthetic(400)
        assert log_loss(labels, ConstantBaseline().predict(rows)) == pytest.approx(
            math.log(2), abs=0.01
        )


class TestRadiantBias:
    def test_learns_the_observed_rate(self) -> None:
        """Laplace-smoothed, so it lands near the observed rate rather than exactly on it -
        the smoothing is what stops a degenerate sample producing a certainty."""
        labels = [True] * 55 + [False] * 45
        model = RadiantBiasBaseline.fit(labels)
        assert model.p == pytest.approx(0.55, abs=0.01)

    def test_smoothing_vanishes_on_a_real_sample(self) -> None:
        labels = [True] * 5500 + [False] * 4500
        assert RadiantBiasBaseline.fit(labels).p == pytest.approx(0.55, abs=0.001)

    def test_beats_the_constant_when_a_bias_exists(self) -> None:
        labels = [True] * 700 + [False] * 300
        rows = [{"gold_adv": 0.0, "minute": 10.0} for _ in labels]
        biased = RadiantBiasBaseline.fit(labels)
        assert log_loss(labels, biased.predict(rows)) < log_loss(
            labels, ConstantBaseline().predict(rows)
        )

    def test_a_degenerate_sample_does_not_produce_certainty(self) -> None:
        """All-wins training data must not yield p=1.0 - one surprise then scores infinity."""
        model = RadiantBiasBaseline.fit([True] * 50)
        assert 0.5 < model.p < 1.0


class TestGoldMinuteLogistic:
    def test_beats_the_constant_baseline(self) -> None:
        rows, labels = synthetic()
        model = GoldMinuteLogistic.fit(rows, labels)
        assert log_loss(labels, model.predict(rows)) < log_loss(
            labels, ConstantBaseline().predict(rows)
        )

    def test_a_gold_lead_raises_the_probability(self) -> None:
        rows, labels = synthetic()
        model = GoldMinuteLogistic.fit(rows, labels)
        ahead = model.predict([{"gold_adv": 10000.0, "minute": 30.0}])[0]
        behind = model.predict([{"gold_adv": -10000.0, "minute": 30.0}])[0]
        assert ahead > 0.5 > behind

    def test_the_same_lead_matters_more_later(self) -> None:
        """Without the interaction term this baseline is artificially weak, and the gate it
        guards becomes easy to pass for the wrong reason."""
        rows, labels = synthetic()
        model = GoldMinuteLogistic.fit(rows, labels)
        early = model.predict([{"gold_adv": 8000.0, "minute": 5.0}])[0]
        late = model.predict([{"gold_adv": 8000.0, "minute": 45.0}])[0]
        assert late > early

    def test_predictions_stay_inside_the_open_unit_interval(self) -> None:
        rows, labels = synthetic()
        model = GoldMinuteLogistic.fit(rows, labels)
        extreme = model.predict(
            [{"gold_adv": 1e9, "minute": 200.0}, {"gold_adv": -1e9, "minute": 200.0}]
        )
        assert all(0.0 < p < 1.0 for p in extreme)

    def test_fitting_is_deterministic(self) -> None:
        rows, labels = synthetic(800)
        first = GoldMinuteLogistic.fit(rows, labels).predict(rows)
        second = GoldMinuteLogistic.fit(rows, labels).predict(rows)
        assert first == second

    def test_missing_features_are_treated_as_zero(self) -> None:
        """The live path can hand over a state with no gold reading yet."""
        rows, labels = synthetic(400)
        model = GoldMinuteLogistic.fit(rows, labels)
        assert 0.0 < model.predict([{}])[0] < 1.0
