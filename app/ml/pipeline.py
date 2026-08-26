"""Offline pipeline entrypoints (spec sections 5, 7, phases 3-4).

featurize -> train -> validate -> register. Run as scripts, never from the API process.

Non-negotiables baked into the design, not left to discipline:
  - splits are walk-forward in time and grouped by match_id; random splits are forbidden
  - a frozen holdout of the last 2-3 months is never touched during tuning
  - the model must beat the baselines in section 7.3 on EVERY minute bucket
"""

from typing import Any


def featurize() -> None:
    """TODO(phase-3): parsed matches -> match_snapshots (~2M rows)."""
    raise NotImplementedError


def train(config: dict[str, Any]) -> None:
    """TODO(phase-4): LightGBM + calibration on Tier 1."""
    raise NotImplementedError


def validate(model_version: str) -> None:
    """TODO(phase-4): log loss / Brier / ECE by minute bucket vs the baselines."""
    raise NotImplementedError
