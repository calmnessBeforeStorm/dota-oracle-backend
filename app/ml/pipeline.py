"""Offline pipeline (spec sections 5, 7, phase 4).

    load -> split -> train -> calibrate -> evaluate -> register

Run as a script, never from the API process (section 9.3). The API only ever loads a booster
that this pipeline has already written and scored.

Non-negotiables, baked into the code rather than left to discipline:

  - splits are forward in time and grouped by `match_id` (`dataset.split_by_time`)
  - the holdout is scored once, at the end, and nothing is tuned against it
  - a model that does not beat every baseline in every minute bucket is written and reported
    but **never activated** (`evaluate`, and the gate in the model card)

LightGBM is imported inside `train`, not at module scope: everything above this line runs in
the API image, which does not carry the `ml` extra, and importing at the top would break
`app.ml.registry` for the service that only reads model cards.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.core.logging import get_logger
from app.features.live import FEATURE_ORDER, as_vector
from app.ml.baselines import fit_baselines
from app.ml.calibration import IdentityCalibrator, PlattCalibrator
from app.ml.dataset import SnapshotRow, Split, load_snapshots, split_by_time
from app.ml.evaluate import Evaluation, evaluate
from app.ml.registry import ModelCard, new_version, save_card

log = get_logger(__name__)

#: Deliberately modest. Thirty thousand rows from seven hundred matches cannot support a
#: large tree ensemble, and a model that memorises the training slice will fail the gate for
#: the right reason but waste the run. Revisit when the dataset is measured in millions.
DEFAULT_PARAMS: dict[str, Any] = {
    "objective": "binary",
    "metric": "binary_logloss",
    "learning_rate": 0.05,
    "num_leaves": 31,
    "min_data_in_leaf": 200,
    "feature_fraction": 0.8,
    "bagging_fraction": 0.8,
    "bagging_freq": 1,
    "lambda_l2": 1.0,
    "verbose": -1,
    # Determinism: two runs on the same data must produce the same model, or the gate is
    # measuring luck.
    "seed": 42,
    "deterministic": True,
    "force_row_wise": True,
}

DEFAULT_ROUNDS = 400
EARLY_STOPPING_ROUNDS = 40

#: Below this, calibrating does more harm than good, and the number is not a guess.
#: The standard error of a proportion is sqrt(0.25/n): at 70 matches it is six percentage
#: points, so the slice's own Radiant win rate can sit far from the population's - measured
#: at 58.6% on validation against 45.7% on holdout, with a true rate near 51%. Platt then
#: learns that gap as a bias and applies it to data that does not share it; on the run of
#: 27.08.2026 it turned a holdout log loss of 0.5685 into 0.6115. Five hundred matches pins
#: the base rate to about two points, which is small enough to calibrate against.
MIN_CALIBRATION_MATCHES = 500


@dataclass(frozen=True)
class TrainingResult:
    version: str
    card: ModelCard
    evaluation: Evaluation
    booster_path: Path


def _matrix(rows: Sequence[SnapshotRow]) -> tuple[list[list[float]], list[int]]:
    """Feature vectors in `FEATURE_ORDER`, never in dict order (spec section 6.4)."""
    return (
        [as_vector(row.features) for row in rows],
        [1 if row.radiant_win else 0 for row in rows],
    )


def train_booster(split: Split, params: dict[str, Any] | None = None, rounds: int = DEFAULT_ROUNDS):  # type: ignore[no-untyped-def]
    """Fit LightGBM on the training slice, early-stopping on validation.

    Returns the raw booster. Untyped on purpose: annotating it would require importing
    lightgbm at module scope, which is exactly what this module avoids.
    """
    import lightgbm as lgb
    import numpy as np

    x_train, y_train = _matrix(split.train)
    x_val, y_val = _matrix(split.validation)

    # LightGBM takes an ndarray, not a list of lists. numpy is imported here rather than at
    # module scope for the same reason lightgbm is: the API image has neither.
    train_set = lgb.Dataset(
        np.asarray(x_train, dtype=np.float64),
        label=np.asarray(y_train, dtype=np.int32),
        feature_name=list(FEATURE_ORDER),
    )
    val_set = lgb.Dataset(
        np.asarray(x_val, dtype=np.float64),
        label=np.asarray(y_val, dtype=np.int32),
        reference=train_set,
    )

    booster = lgb.train(
        {**DEFAULT_PARAMS, **(params or {})},
        train_set,
        num_boost_round=rounds,
        valid_sets=[val_set],
        callbacks=[lgb.early_stopping(EARLY_STOPPING_ROUNDS, verbose=False)],
    )
    log.info("model.trained", best_iteration=booster.best_iteration, rounds=rounds)
    return booster


def _predict(booster: Any, rows: Sequence[SnapshotRow]) -> list[float]:
    import numpy as np

    vectors, _ = _matrix(rows)
    return [float(p) for p in booster.predict(np.asarray(vectors, dtype=np.float64))]


async def train(
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    model_dir: str | Path | None = None,
    params: dict[str, Any] | None = None,
    rounds: int = DEFAULT_ROUNDS,
    notes: str = "",
) -> TrainingResult:
    """The whole phase-4 run. Writes an artifact and a card; never activates anything.

    Activation is a separate, deliberate act: `ACTIVE_MODEL_VERSION` in the environment.
    A pipeline that promotes its own output is a pipeline that eventually promotes a
    regression at three in the morning.
    """
    settings = get_settings()
    session_factory = session_factory or _default_session_factory()
    directory = Path(model_dir or settings.model_dir)

    rows = await load_snapshots(session_factory)
    if not rows:
        raise ValueError("no snapshots to train on - run `ingestion.cli featurize` first")
    split = split_by_time(rows)

    booster = train_booster(split, params, rounds)

    # Calibrate on validation, never on train (the model has already seen it) and never on
    # holdout (which must stay untouched until it is scored once).
    raw_validation = _predict(booster, split.validation)
    validation_labels = [row.radiant_win for row in split.validation]
    validation_matches = len({row.match_id for row in split.validation})

    calibrator: PlattCalibrator | IdentityCalibrator
    if validation_matches < MIN_CALIBRATION_MATCHES:
        calibrator = IdentityCalibrator()
        calibrator_name = f"identity (validation {validation_matches} matches, too small)"
        log.warning(
            "model.calibration_skipped",
            validation_matches=validation_matches,
            required=MIN_CALIBRATION_MATCHES,
        )
    else:
        calibrator = PlattCalibrator.fit(raw_validation, validation_labels)
        calibrator_name = "platt"

    holdout_probs = calibrator.apply(_predict(booster, split.holdout))
    holdout_labels = [row.radiant_win for row in split.holdout]
    holdout_minutes = [row.minute for row in split.holdout]

    # Baselines are fitted on train and scored on holdout, exactly like the candidate.
    # Fitting them on holdout would let them peek at the answers the model cannot see.
    train_features = [row.features for row in split.train]
    train_labels = [row.radiant_win for row in split.train]
    holdout_features = [row.features for row in split.holdout]
    baselines = {
        baseline.name: baseline.predict(holdout_features)
        for baseline in fit_baselines(train_features, train_labels)
    }

    result = evaluate(holdout_labels, holdout_probs, holdout_minutes, baselines)

    version = new_version()
    summary = split.summary()
    card = ModelCard(
        version=version,
        trained_at=datetime.now(UTC),
        train_window=_window(summary["train_window"]),
        holdout_window=_window(summary["holdout_window"]),
        train_matches=int(summary["train_matches"]),
        train_rows=int(summary["train_rows"]),
        holdout_matches=int(summary["holdout_matches"]),
        holdout_rows=int(summary["holdout_rows"]),
        feature_order=FEATURE_ORDER,
        holdout_log_loss=result.log_loss,
        holdout_brier=result.brier,
        holdout_ece=result.ece,
        log_loss_by_minute=result.log_loss_by_minute,
        baseline_log_loss_by_minute=result.baseline_log_loss_by_minute,
        gate_failures=result.failures,
        calibrator=calibrator_name,
        notes=notes,
    )

    artifact = _write_artifacts(directory, version, booster, card)

    log.info("model.run_finished", version=version, **result.as_log_fields())
    return TrainingResult(version=version, card=card, evaluation=result, booster_path=artifact)


def _window(text: str) -> tuple[str, str]:
    """`"2026-07-01 .. 2026-08-01"` -> both ends. `"-"` for an empty slice stays honest."""
    parts = text.split(" .. ")
    return (parts[0], parts[-1])


def _write_artifacts(directory: Path, version: str, booster: Any, card: ModelCard) -> Path:
    """Blocking disk writes, kept in one sync place.

    This pipeline is a script, so blocking is fine and expected - but scattering file IO
    through an async function is how blocking calls end up in a server by copy-paste.
    """
    directory.mkdir(parents=True, exist_ok=True)
    artifact = directory / f"{version}.txt"
    booster.save_model(str(artifact))
    save_card(directory, card)
    return artifact


def _default_session_factory() -> async_sessionmaker[AsyncSession]:
    from app.db.session import get_session_factory

    return get_session_factory()
