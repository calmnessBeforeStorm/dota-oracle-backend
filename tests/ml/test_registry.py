"""The model registry (spec sections 4.3, 9.3).

`predictions.model_version` is the only way to explain a quality drop a month later, so an
artifact with no card is not a model and must not be reachable as one.
"""

from datetime import UTC, datetime
from pathlib import Path

from app.ml.registry import (
    ModelCard,
    latest_passing,
    list_models,
    load_card,
    new_version,
    save_card,
)


def card(version: str, *, failures: list[str] | None = None, day: int = 1) -> ModelCard:
    return ModelCard(
        version=version,
        trained_at=datetime(2026, 8, day, tzinfo=UTC),
        train_window=("2026-07-01", "2026-08-01"),
        holdout_window=("2026-08-02", "2026-08-25"),
        train_matches=490,
        train_rows=21000,
        holdout_matches=140,
        holdout_rows=6000,
        feature_order=("minute", "gold_adv"),
        holdout_log_loss=0.51,
        holdout_brier=0.17,
        holdout_ece=0.03,
        log_loss_by_minute={"0-4": 0.69, "30+": 0.31},
        baseline_log_loss_by_minute={"constant-0.5": {"0-4": 0.693, "30+": 0.693}},
        gate_failures=failures or [],
    )


class TestVersions:
    def test_a_version_is_timestamped_so_runs_never_collide(self) -> None:
        first = new_version(now=datetime(2026, 8, 27, 10, 0, 0, tzinfo=UTC))
        second = new_version(now=datetime(2026, 8, 27, 10, 0, 1, tzinfo=UTC))
        assert first != second
        assert first.startswith("lgbm-")


class TestRoundTrip:
    def test_a_card_survives_being_written_and_read(self, tmp_path: Path) -> None:
        original = card("lgbm-1")
        save_card(tmp_path, original)

        assert load_card(tmp_path, "lgbm-1") == original

    def test_the_gate_verdict_is_derived_not_stored(self, tmp_path: Path) -> None:
        """Storing it as its own field invites the two disagreeing."""
        save_card(tmp_path, card("lgbm-1", failures=["0-4: does not beat constant-0.5"]))

        loaded = load_card(tmp_path, "lgbm-1")

        assert loaded.passes_gate is False
        assert loaded.gate_failures == ["0-4: does not beat constant-0.5"]

    def test_the_json_carries_the_verdict_for_humans(self, tmp_path: Path) -> None:
        path = save_card(tmp_path, card("lgbm-1"))
        assert '"passes_gate": true' in path.read_text(encoding="utf-8")


class TestListing:
    def test_models_come_back_newest_first(self, tmp_path: Path) -> None:
        save_card(tmp_path, card("lgbm-old", day=1))
        save_card(tmp_path, card("lgbm-new", day=20))

        assert [c.version for c in list_models(tmp_path)] == ["lgbm-new", "lgbm-old"]

    def test_a_missing_directory_lists_nothing(self, tmp_path: Path) -> None:
        assert list_models(tmp_path / "nope") == []

    def test_an_unreadable_card_does_not_hide_the_good_ones(self, tmp_path: Path) -> None:
        save_card(tmp_path, card("lgbm-good"))
        (tmp_path / "lgbm-broken.card.json").write_text("{not json", encoding="utf-8")

        assert [c.version for c in list_models(tmp_path)] == ["lgbm-good"]

    def test_latest_passing_skips_models_that_failed_the_gate(self, tmp_path: Path) -> None:
        """The newest model is not automatically the one to deploy."""
        save_card(tmp_path, card("lgbm-good", day=1))
        save_card(tmp_path, card("lgbm-newer-but-bad", day=20, failures=["0-4: worse"]))

        best = latest_passing(tmp_path)

        assert best is not None
        assert best.version == "lgbm-good"

    def test_latest_passing_is_none_when_nothing_qualifies(self, tmp_path: Path) -> None:
        save_card(tmp_path, card("lgbm-bad", failures=["0-4: worse"]))
        assert latest_passing(tmp_path) is None
