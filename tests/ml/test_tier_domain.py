"""Section 5.4: training on the whole corpus, honest about Tier 1.

The spec offers sample weights or calibration on the target domain, and prefers the second.
Both are here; the second is the one that runs, and only when there is enough Tier 1 to
calibrate against - a small target slice teaches Platt its own sampling error as a bias, which
on 2026-08-27 turned a holdout log loss of 0.5685 into 0.6115.
"""

from datetime import UTC, datetime, timedelta

from app.ml.dataset import SnapshotRow
from app.ml.pipeline import (
    MIN_CALIBRATION_MATCHES,
    TIER_WEIGHTS,
    WEIGHT_HALF_LIFE_DAYS,
    calibration_rows,
    tier_weights,
)

NEWEST = datetime(2026, 9, 1, tzinfo=UTC)


def rows(count: int, tier: str, first_id: int = 0, age_days: float = 0.0) -> list[SnapshotRow]:
    return [
        SnapshotRow(
            match_id=first_id + i,
            minute=10,
            features={},
            radiant_win=True,
            start_time=NEWEST - timedelta(days=age_days),
            tier=tier,
        )
        for i in range(count)
    ]


class TestWeights:
    def test_tier_one_outweighs_the_lower_tiers(self) -> None:
        sample = rows(1, "tier1") + rows(1, "tier2", 1) + rows(1, "tier3", 2)
        weights = tier_weights(sample)

        assert weights[0] > weights[1] > weights[2]
        assert weights[0] == TIER_WEIGHTS["tier1"]

    def test_an_unmapped_league_is_not_penalised(self) -> None:
        """72% of the archive is unmapped. Down-weighting it would weight by how far the
        Liquipedia mapping has got, not by how good the match was."""
        assert tier_weights(rows(1, "unknown")) == tier_weights(rows(1, "tier1"))

    def test_age_halves_the_weight_at_the_half_life(self) -> None:
        old = tier_weights(rows(1, "tier1") + rows(1, "tier1", 1, WEIGHT_HALF_LIFE_DAYS))

        assert old[0] == 1.0
        assert abs(old[1] - 0.5) < 1e-9

    def test_no_rows_no_weights(self) -> None:
        assert tier_weights([]) == []

    def test_weighting_is_on_by_default(self) -> None:
        """Measured, not assumed: weighted beat unweighted in the three late buckets and tied
        in the four early ones, on the same split with the difference bootstrapped by match."""
        import inspect

        from app.ml.pipeline import train, train_booster

        assert inspect.signature(train_booster).parameters["weighted"].default is True
        assert inspect.signature(train).parameters["weighted"].default is True


class TestCalibrationDomain:
    def test_tier_one_is_used_when_there_is_enough_of_it(self) -> None:
        sample = rows(MIN_CALIBRATION_MATCHES, "tier1") + rows(50, "unknown", 10_000)

        chosen, label = calibration_rows(sample)

        assert {row.tier for row in chosen} == {"tier1"}
        assert "tier1" in label

    def test_it_falls_back_to_every_tier_and_says_so(self) -> None:
        """The case today: Tier 1 is a subset of validation, so it hits the floor first."""
        sample = rows(10, "tier1") + rows(MIN_CALIBRATION_MATCHES, "unknown", 10_000)

        chosen, label = calibration_rows(sample)

        assert len(chosen) == len(sample)
        assert "all tiers" in label
        # The card has to record what was missing, or the fallback is invisible.
        assert str(MIN_CALIBRATION_MATCHES) in label

    def test_too_little_of_anything_means_no_calibration_at_all(self) -> None:
        chosen, label = calibration_rows(rows(10, "tier1"))

        assert chosen == []
        assert label.startswith("identity")

    def test_the_domain_is_counted_in_matches_not_rows(self) -> None:
        """Invariant 3. Forty snapshots of one game are one observation, and a floor counted
        in rows would clear itself on a dozen matches."""
        one_match = [
            SnapshotRow(
                match_id=7,
                minute=m,
                features={},
                radiant_win=True,
                start_time=NEWEST,
                tier="tier1",
            )
            for m in range(MIN_CALIBRATION_MATCHES * 2)
        ]

        chosen, label = calibration_rows(one_match)

        assert chosen == []
        assert label.startswith("identity")
