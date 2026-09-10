"""The gate must score the configuration that is actually served (spec sections 6.4, 11).

Until 10.09.2026 it did not. The holdout came out of `match_snapshots`, where the pre-match
block is filled with real values; the live path fills none of it, because the poller calls
`from_live_league_game` without `prematch` or `prematch_prior`. So every card measured a
model that does not exist in production, and it measured it favourably: on
`lgbm-20260901-102407`, 1575 of 2190 splits stood on features that arrive at serving time as
constants.

The failure is silent by construction. A model scored on inputs it will never see reports
the same shape of number as one scored honestly - right range, plausible curve, gate passed -
so nothing downstream can tell the two apart. That is why this is checked here rather than
watched for on the dashboard.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.features.live import FEATURE_ORDER, PREMATCH_BLOCK, SERVED_FEATURES
from app.ml.dataset import SnapshotRow

NEWEST = datetime(2026, 9, 1, tzinfo=UTC)


def features_with(**overrides: float) -> dict[str, float]:
    """A complete vector, zero everywhere except what a test cares about."""
    return dict.fromkeys(FEATURE_ORDER, 0.0) | overrides


class TestTheServingView:
    def test_it_substitutes_what_the_live_path_substitutes(self) -> None:
        """The values are read out of the builder, not restated here.

        Restating them is how the comment above the defaults went stale: it described
        `skill_sigma_sum` as a difference between the sides, which it is not.
        """
        from app.features.live import serving_view

        served = serving_view(features_with(prematch_prior=0.83, skill_diff=4.0))

        assert served["prematch_prior"] == 0.5
        assert served["skill_diff"] == 0.0

    def test_it_leaves_the_features_the_live_path_supplies_alone(self) -> None:
        from app.features.live import serving_view

        served = serving_view(features_with(gold_adv=1500.0, minute=20.0, tower_diff=-3.0))

        assert served["gold_adv"] == 1500.0
        assert served["minute"] == 20.0
        assert served["tower_diff"] == -3.0

    def test_it_covers_the_whole_pre_match_block(self) -> None:
        from app.features.live import serving_view

        filled = features_with(**dict.fromkeys(PREMATCH_BLOCK, 7.0))
        served = serving_view(filled)

        assert not [name for name in PREMATCH_BLOCK if served[name] == 7.0]


class TestTheGateScoresWhatIsServed:
    """The end-to-end version: a model whose only signal disappears at serving time.

    `prematch_prior` here predicts the label perfectly. A gate that scores the training
    configuration reports a log loss near zero and passes the model; a gate that scores what
    production would actually run reports a coin flip, because the prior arrives as 0.5 in
    every match. The second is the truth about that model.
    """

    def rows(self) -> list[SnapshotRow]:
        out: list[SnapshotRow] = []
        for match in range(120):
            radiant_win = match % 2 == 0
            # The only signal in the vector, and one the live path cannot supply.
            prior = 0.95 if radiant_win else 0.05
            for minute in range(1, 6):
                out.append(
                    SnapshotRow(
                        match_id=match,
                        minute=minute,
                        features=features_with(
                            prematch_prior=prior, minute=float(minute), log_minute=1.0
                        ),
                        radiant_win=radiant_win,
                        start_time=NEWEST - timedelta(days=200 - match),
                        tier="tier1",
                    )
                )
        return out

    @pytest.mark.asyncio
    async def test_a_signal_that_vanishes_at_serving_is_reported_as_vanished(
        self, tmp_path, monkeypatch
    ) -> None:
        pytest.importorskip("lightgbm")

        from app.ml import pipeline

        prepared = self.rows()

        async def fake_load(_factory: object) -> list[SnapshotRow]:
            return prepared

        monkeypatch.setattr(pipeline, "load_snapshots", fake_load)

        result = await pipeline.train(
            session_factory=object(),  # never used: load_snapshots is patched
            model_dir=tmp_path,
            rounds=40,
            feature_names=FEATURE_ORDER,  # the configuration that is NOT servable
        )

        # A coin flip, not the near-zero the training configuration would report.
        assert result.card.holdout_log_loss > 0.6

    @pytest.mark.asyncio
    async def test_the_default_feature_set_is_the_one_the_live_path_supplies(self) -> None:
        """A model is trained on what can be served unless someone deliberately says otherwise."""
        import inspect

        from app.ml.pipeline import train

        assert inspect.signature(train).parameters["feature_names"].default == SERVED_FEATURES
