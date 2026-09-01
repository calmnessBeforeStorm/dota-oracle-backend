"""Model registry (spec sections 4.3, 9.3, phase 4).

Whatever is served must be identifiable. `predictions.model_version` is the only way to
explain a quality drop a month after the fact, so a model with no version never gets served
and a version with no card is not a model.

The spec sketches MLflow and object storage. Neither is here yet, and adding them to track
one run on thirty thousand rows would be infrastructure for its own sake. A card is a JSON
file next to its booster, which satisfies the actual requirement - every artifact carries
what it was trained on and how it scored - and can be replaced by MLflow later without the
serving path noticing.
"""

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.logging import get_logger

log = get_logger(__name__)

CARD_SUFFIX = ".card.json"
BOOSTER_SUFFIX = ".txt"


@dataclass(frozen=True)
class ModelCard:
    version: str
    trained_at: datetime
    train_window: tuple[str, str]
    holdout_window: tuple[str, str]
    train_matches: int
    train_rows: int
    holdout_matches: int
    holdout_rows: int
    feature_order: tuple[str, ...]
    holdout_log_loss: float
    holdout_brier: float
    holdout_ece: float
    #: Per minute bucket - the averaged number lies, minute 40 is trivial (section 7.2).
    log_loss_by_minute: dict[str, float]
    baseline_log_loss_by_minute: dict[str, dict[str, float]]
    #: Empty means the model beat every baseline in every bucket and may be served.
    gate_failures: list[str] = field(default_factory=list)
    calibrator: str = "platt"
    #: The fitted calibration, `sigmoid(a * logit(p_raw) + b)`, so the serving path can apply
    #: the same transform the evaluation did. Recording only the *name* was not enough and
    #: was not obviously not enough: the card said "platt", the reported metrics were
    #: post-calibration, and the booster on disk was raw - so promoting a model served
    #: something that had never been scored. Defaults are the identity, which is what an old
    #: card without these fields describes and what `IdentityCalibrator` does.
    calibrator_a: float = 1.0
    calibrator_b: float = 0.0
    notes: str = ""

    @property
    def passes_gate(self) -> bool:
        return not self.gate_failures

    def to_json(self) -> str:
        payload: dict[str, Any] = asdict(self)
        payload["trained_at"] = self.trained_at.isoformat()
        payload["train_window"] = list(self.train_window)
        payload["holdout_window"] = list(self.holdout_window)
        payload["feature_order"] = list(self.feature_order)
        payload["passes_gate"] = self.passes_gate
        return json.dumps(payload, indent=2, ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> "ModelCard":
        payload = json.loads(text)
        payload.pop("passes_gate", None)  # derived, not stored state
        payload["trained_at"] = datetime.fromisoformat(payload["trained_at"])
        payload["train_window"] = tuple(payload["train_window"])
        payload["holdout_window"] = tuple(payload["holdout_window"])
        payload["feature_order"] = tuple(payload["feature_order"])
        # Cards written before the coefficients were stored describe the identity transform,
        # which is what the serving path did with them anyway.
        payload.setdefault("calibrator_a", 1.0)
        payload.setdefault("calibrator_b", 0.0)
        return cls(**payload)


def new_version(prefix: str = "lgbm", now: datetime | None = None) -> str:
    """A version is a timestamp, so two runs on the same day never overwrite each other."""
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%d-%H%M%S")
    return f"{prefix}-{stamp}"


def card_path(model_dir: str | Path, version: str) -> Path:
    return Path(model_dir) / f"{version}{CARD_SUFFIX}"


def booster_path(model_dir: str | Path, version: str) -> Path:
    return Path(model_dir) / f"{version}{BOOSTER_SUFFIX}"


def save_card(model_dir: str | Path, card: ModelCard) -> Path:
    path = card_path(model_dir, card.version)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(card.to_json(), encoding="utf-8")
    log.info("model.card_written", version=card.version, passes_gate=card.passes_gate)
    return path


def load_card(model_dir: str | Path, version: str) -> ModelCard:
    return ModelCard.from_json(card_path(model_dir, version).read_text(encoding="utf-8"))


def list_models(model_dir: str | Path) -> list[ModelCard]:
    """Every card in the directory, newest first. Unreadable cards are skipped loudly."""
    directory = Path(model_dir)
    if not directory.exists():
        return []

    cards: list[ModelCard] = []
    for path in sorted(directory.glob(f"*{CARD_SUFFIX}")):
        try:
            cards.append(ModelCard.from_json(path.read_text(encoding="utf-8")))
        except Exception as exc:
            # A malformed card must not hide the models that are fine, but it must be
            # visible: silently returning fewer models than exist is its own kind of bug.
            log.error("model.card_unreadable", path=str(path), error=str(exc))
    return sorted(cards, key=lambda card: card.trained_at, reverse=True)


def latest_passing(model_dir: str | Path) -> ModelCard | None:
    """The newest model that cleared the gate, or None. What a deploy should reach for."""
    return next((card for card in list_models(model_dir) if card.passes_gate), None)
