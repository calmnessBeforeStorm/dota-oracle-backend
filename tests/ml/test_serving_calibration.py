"""What gets served has to be what was scored.

Training fits a calibrator on the validation slice and applies it before measuring anything:
the holdout log loss on the card, the ECE, the gate verdict. The card recorded the
calibrator's *name* and threw its coefficients away, and the serving path loaded the bare
booster - so promoting a model would have served a function nobody had ever evaluated.

Nothing about that looks wrong from outside. Same feature vector, same version string,
probabilities in the right range, a card saying "platt". The only way to catch it is to
insist that the two paths agree, which is what these tests do.
"""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from app.ml.calibration import IdentityCalibrator, PlattCalibrator
from app.ml.registry import ModelCard, load_card, save_card
from tests.ml.test_registry import card as make_card


def test_a_fitted_calibrator_survives_the_round_trip(tmp_path: Path) -> None:
    fitted = PlattCalibrator(a=1.013370, b=-0.004753)
    base = make_card("lgbm-20260901-000000")
    card = replace(base, calibrator_a=fitted.a, calibrator_b=fitted.b)
    save_card(tmp_path, card)

    restored = load_card(tmp_path, card.version)
    rebuilt = PlattCalibrator(a=restored.calibrator_a, b=restored.calibrator_b)

    assert rebuilt.apply([0.9, 0.5, 0.02]) == fitted.apply([0.9, 0.5, 0.02])


def test_a_card_without_coefficients_reads_as_the_identity(tmp_path: Path) -> None:
    """Every card written before this existed. They served the raw booster, so that is what
    they have to keep describing - inventing a transform for them would change history."""
    card = make_card("lgbm-20260901-000001")
    payload = json.loads(card.to_json())
    del payload["calibrator_a"]
    del payload["calibrator_b"]
    path = tmp_path / f"{card.version}.card.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    restored = ModelCard.from_json(path.read_text(encoding="utf-8"))

    assert restored.calibrator_a == 1.0
    assert restored.calibrator_b == 0.0
    identity = PlattCalibrator(a=restored.calibrator_a, b=restored.calibrator_b)
    for p in (0.02, 0.4, 0.5, 0.97):
        assert identity.apply_one(p) == pytest.approx(p, abs=1e-9)


def test_both_calibrators_expose_the_coefficients_the_card_stores() -> None:
    """`train` writes `calibrator.a` and `.b` without asking which kind it got."""
    assert (IdentityCalibrator().a, IdentityCalibrator().b) == (1.0, 0.0)
    assert (PlattCalibrator(a=2.0, b=1.0).a, PlattCalibrator(a=2.0, b=1.0).b) == (2.0, 1.0)


def test_apply_one_matches_apply() -> None:
    """The serving path has one probability at a time; it must not drift from the batch path."""
    calibrator = PlattCalibrator(a=0.83, b=0.11)
    probabilities = [0.01, 0.25, 0.5, 0.75, 0.99]

    assert [calibrator.apply_one(p) for p in probabilities] == calibrator.apply(probabilities)


def test_identity_apply_one_matches_apply() -> None:
    calibrator = IdentityCalibrator()
    probabilities = [0.01, 0.5, 0.99]

    assert [calibrator.apply_one(p) for p in probabilities] == calibrator.apply(probabilities)
