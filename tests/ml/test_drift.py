"""Phase 7: the calibration drift alarm (spec sections 11, 12).

The failure this watches for is quiet by design. A model whose meta has moved still ranks
the right side ahead, so log loss barely stirs and nothing on the site looks broken; what
goes first is how far ahead it claims that side is.

Which makes the alarm itself the thing that has to be right. An alarm that cries wolf gets
muted, and a muted alarm is worse than none - it costs the same and reassures.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.api.accuracy import ScoredPrediction
from app.ml.drift import (
    MIN_MATCHES,
    NOISE_AT_85,
    check_drift,
    noise_floor,
)

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)


def rows(
    *,
    matches: int,
    days_ago: float,
    promised: float,
    observed: float,
    first_match_id: int = 0,
) -> list[ScoredPrediction]:
    """`matches` games, each one prediction, promising `promised` and winning `observed` of
    the time. One row per match keeps the match count and the row count equal, so a test
    that means "forty matches" cannot accidentally mean "forty snapshots of one game"."""
    at = NOW - timedelta(days=days_ago)
    wins = round(matches * observed)
    return [
        ScoredPrediction(
            match_id=first_match_id + index,
            minute=10,
            p_radiant=promised,
            radiant_win=index < wins,
            predicted_at=at,
        )
        for index in range(matches)
    ]


class TestNoiseFloor:
    """The floor is derived from the window, not chosen.

    Measured by splitting one model's own matches into random halves 200 times and comparing
    the two ECEs: p90 gap 0.077 at 85 matches a side, 0.098 at 50, 0.135 at 25.
    """

    def test_it_reproduces_the_measurement_it_was_fitted_to(self) -> None:
        assert noise_floor(85) == pytest.approx(NOISE_AT_85)

    def test_it_matches_the_other_two_measured_points(self) -> None:
        """Not fitted to these, so agreeing with them is evidence the shape is right."""
        assert noise_floor(50) == pytest.approx(0.098, abs=0.01)
        assert noise_floor(25) == pytest.approx(0.135, abs=0.01)

    def test_it_falls_as_evidence_accumulates(self) -> None:
        """The whole reason it is a function. A constant chosen today goes deaf: at ten
        thousand matches a real drift of 0.03 would sit far under a floor set for eighty."""
        assert noise_floor(5000) < noise_floor(500) < noise_floor(85)
        assert noise_floor(5000) < 0.02

    def test_an_empty_window_is_refused(self) -> None:
        with pytest.raises(ValueError, match="at least one match"):
            noise_floor(0)


class TestNotEnoughData:
    def test_no_recent_predictions_is_not_a_verdict(self) -> None:
        verdict = check_drift("v1", rows(matches=200, days_ago=30, promised=0.9, observed=0.9), NOW)

        assert verdict.verdict == "not enough data"
        assert not verdict.is_alerting

    def test_no_history_is_not_a_verdict_either(self) -> None:
        """A model served for the first time this week has nothing to have drifted from."""
        verdict = check_drift("v1", rows(matches=200, days_ago=1, promised=0.9, observed=0.9), NOW)

        assert verdict.verdict == "not enough data"

    def test_a_handful_of_matches_is_not_evidence(self) -> None:
        """Measured on the real log: the same model scored 0.070 log loss in the 30+ bucket
        on 14 matches and 2.356 on 171."""
        scored = [
            *rows(matches=MIN_MATCHES - 1, days_ago=1, promised=0.9, observed=0.2),
            *rows(matches=500, days_ago=30, promised=0.9, observed=0.9, first_match_id=10_000),
        ]

        verdict = check_drift("v1", scored, NOW)

        assert verdict.verdict == "not enough data"
        assert "matches in the smaller window" in verdict.detail

    def test_it_still_reports_what_it_saw(self) -> None:
        """A refusal is not silence: the windows are attached so the reader can see how close
        the check is to being able to answer."""
        scored = [
            *rows(matches=5, days_ago=1, promised=0.9, observed=0.9),
            *rows(matches=500, days_ago=30, promised=0.9, observed=0.9, first_match_id=10_000),
        ]

        verdict = check_drift("v1", scored, NOW)

        assert verdict.recent is not None and verdict.recent.matches == 5
        assert verdict.reference is not None and verdict.reference.matches == 500


class TestVerdicts:
    def test_a_steady_model_does_not_alert(self) -> None:
        scored = [
            *rows(matches=300, days_ago=1, promised=0.7, observed=0.7),
            *rows(matches=300, days_ago=30, promised=0.7, observed=0.7, first_match_id=10_000),
        ]

        verdict = check_drift("v1", scored, NOW)

        assert verdict.verdict == "ok"
        assert not verdict.is_alerting

    def test_calibration_falling_apart_alerts(self) -> None:
        """Promising 90% and winning 50% is the failure the whole page exists to surface."""
        scored = [
            *rows(matches=300, days_ago=1, promised=0.9, observed=0.5),
            *rows(matches=300, days_ago=30, promised=0.9, observed=0.9, first_match_id=10_000),
        ]

        verdict = check_drift("v1", scored, NOW)

        assert verdict.is_alerting
        assert verdict.threshold is not None and verdict.threshold < 0.4

    def test_a_model_that_got_better_is_not_an_alert(self) -> None:
        """Reported, because a sudden improvement is usually a data problem rather than a
        gift, but never as a drift alarm."""
        scored = [
            *rows(matches=300, days_ago=1, promised=0.9, observed=0.9),
            *rows(matches=300, days_ago=30, promised=0.9, observed=0.5, first_match_id=10_000),
        ]

        verdict = check_drift("v1", scored, NOW)

        assert verdict.verdict == "improving"
        assert not verdict.is_alerting

    def test_a_permanently_bad_model_is_not_drifting(self) -> None:
        """The reason the check is a comparison and not a threshold. The served baseline sits
        near 0.056 forever; a fixed limit either never fires or fires every day."""
        scored = [
            *rows(matches=300, days_ago=1, promised=0.9, observed=0.6),
            *rows(matches=300, days_ago=30, promised=0.9, observed=0.6, first_match_id=10_000),
        ]

        verdict = check_drift("v1", scored, NOW)

        assert verdict.verdict == "ok"

    def test_a_small_wobble_stays_under_the_floor(self) -> None:
        scored = [
            *rows(matches=300, days_ago=1, promised=0.7, observed=0.71),
            *rows(matches=300, days_ago=30, promised=0.7, observed=0.70, first_match_id=10_000),
        ]

        verdict = check_drift("v1", scored, NOW)

        assert verdict.verdict == "ok"

    def test_the_threshold_tightens_with_the_smaller_window(self) -> None:
        """A precise reference tells you nothing when the recent side is fifty games."""
        narrow = check_drift(
            "v1",
            [
                *rows(matches=50, days_ago=1, promised=0.7, observed=0.7),
                *rows(matches=3000, days_ago=30, promised=0.7, observed=0.7, first_match_id=10_000),
            ],
            NOW,
        )
        wide = check_drift(
            "v1",
            [
                *rows(matches=3000, days_ago=1, promised=0.7, observed=0.7),
                *rows(matches=3000, days_ago=30, promised=0.7, observed=0.7, first_match_id=10_000),
            ],
            NOW,
        )

        assert narrow.threshold is not None and wide.threshold is not None
        assert narrow.threshold > wide.threshold


class TestTheWindowsThemselves:
    def test_windows_are_counted_in_matches_not_rows(self) -> None:
        """Section 5.1 again: forty snapshots of one game are forty views of one outcome, and
        a window measured in rows would call sixty correlated rows a large sample."""
        one_match = [
            ScoredPrediction(
                match_id=7,
                minute=minute,
                p_radiant=0.7,
                radiant_win=True,
                predicted_at=NOW - timedelta(days=1),
            )
            for minute in range(60)
        ]
        scored = [
            *one_match,
            *rows(matches=300, days_ago=30, promised=0.7, observed=0.7, first_match_id=10_000),
        ]

        verdict = check_drift("v1", scored, NOW)

        assert verdict.recent is not None
        assert verdict.recent.matches == 1
        assert verdict.recent.rows == 60
        assert verdict.verdict == "not enough data"

    def test_the_split_uses_when_we_predicted_not_when_the_result_arrived(self) -> None:
        """A model that went bad in July went bad in July, whatever date the outcome landed
        on - and outcomes arrive in bulk whenever the backfill reaches them."""
        scored = [
            *rows(matches=300, days_ago=0.5, promised=0.9, observed=0.5),
            *rows(matches=300, days_ago=8, promised=0.9, observed=0.9, first_match_id=10_000),
        ]

        verdict = check_drift("v1", scored, NOW, recent_days=7)

        assert verdict.recent is not None and verdict.recent.matches == 300
        assert verdict.is_alerting
