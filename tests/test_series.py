"""Series rules, including Bo2 and draws (spec section 5.5)."""

import pytest

from app.db.models.enums import SeriesFormat
from app.domain.series import (
    bo2_naive_outcome_probs,
    is_conditional_game,
    resolve_format,
    series_outcome,
)


class TestResolveFormat:
    def test_stage_default_beats_valve_hint(self) -> None:
        # Valve reports 0 (Bo1) for Bo2 series; the Liquipedia stage is the source of truth.
        assert resolve_format(SeriesFormat.BO2, valve_series_type=0) is SeriesFormat.BO2

    def test_series_override_beats_stage(self) -> None:
        """Tiebreakers and replays deviate from the stage default."""
        assert resolve_format(SeriesFormat.BO2, SeriesFormat.BO3) is SeriesFormat.BO3

    def test_valve_hint_used_only_when_stage_unknown(self) -> None:
        assert resolve_format(None, valve_series_type=1) is SeriesFormat.BO3
        assert resolve_format(None, valve_series_type=2) is SeriesFormat.BO5

    def test_valve_hint_can_never_yield_bo2(self) -> None:
        """Bo2 is not representable in series_type - it must never be inferred from Valve."""
        for series_type in (None, 0, 1, 2, 99):
            assert resolve_format(None, valve_series_type=series_type) is not SeriesFormat.BO2


class TestConditionalGame:
    def test_bo2_second_game_is_never_conditional(self) -> None:
        """Game 2 of a Bo2 is always played - that is what makes Bo2 unbiased material."""
        assert is_conditional_game(SeriesFormat.BO2, 2) is False

    def test_bo3_third_game_is_conditional(self) -> None:
        """Bo3 game 3 happens only at 1-1, so its sample is skewed toward even matchups."""
        assert is_conditional_game(SeriesFormat.BO3, 2) is False
        assert is_conditional_game(SeriesFormat.BO3, 3) is True

    def test_bo5_games_four_and_five_are_conditional(self) -> None:
        assert is_conditional_game(SeriesFormat.BO5, 3) is False
        assert is_conditional_game(SeriesFormat.BO5, 4) is True
        assert is_conditional_game(SeriesFormat.BO5, 5) is True


class TestSeriesOutcome:
    def test_bo2_one_one_is_a_draw(self) -> None:
        outcome = series_outcome(SeriesFormat.BO2, 1, 1)
        assert outcome.is_draw is True
        assert outcome.is_decided is True
        assert outcome.winner_is_a is None

    def test_bo2_two_nil_has_a_winner(self) -> None:
        outcome = series_outcome(SeriesFormat.BO2, 2, 0)
        assert outcome.is_draw is False
        assert outcome.winner_is_a is True

    def test_bo2_after_one_game_is_undecided_not_drawn(self) -> None:
        """1-0 in a Bo2 is not a draw and not a win - the UI must not confuse the two."""
        outcome = series_outcome(SeriesFormat.BO2, 1, 0)
        assert outcome.is_decided is False
        assert outcome.is_draw is False

    @pytest.mark.parametrize(
        ("fmt", "score_a", "score_b", "winner_is_a"),
        [
            (SeriesFormat.BO1, 1, 0, True),
            (SeriesFormat.BO3, 2, 1, True),
            (SeriesFormat.BO3, 0, 2, False),
            (SeriesFormat.BO5, 3, 2, True),
        ],
    )
    def test_non_bo2_formats_never_draw(
        self, fmt: SeriesFormat, score_a: int, score_b: int, winner_is_a: bool
    ) -> None:
        outcome = series_outcome(fmt, score_a, score_b)
        assert outcome.is_draw is False
        assert outcome.winner_is_a is winner_is_a

    def test_only_bo2_can_draw(self) -> None:
        assert SeriesFormat.BO2.can_draw is True
        assert all(not f.can_draw for f in SeriesFormat if f is not SeriesFormat.BO2)


class TestBo2MaxGames:
    def test_a_bo2_series_with_three_maps_cannot_exist(self) -> None:
        """Spec section 12 risk row: guard against a mis-detected Bo2 swallowing a third map."""
        assert SeriesFormat.BO2.max_games == 2
        assert is_conditional_game(SeriesFormat.BO2, 3) is True  # i.e. flagged as impossible


class TestBo2Probabilities:
    def test_naive_distribution_sums_to_one(self) -> None:
        probs = bo2_naive_outcome_probs(0.6)
        assert set(probs) == {"2-0", "1-1", "0-2"}
        assert sum(probs.values()) == pytest.approx(1.0)

    def test_even_teams_favour_a_draw_under_the_naive_model(self) -> None:
        probs = bo2_naive_outcome_probs(0.5)
        assert probs["1-1"] == pytest.approx(0.5)
        # And reality runs higher than this: maps are not independent (spec section 5.5).

    def test_rejects_out_of_range_probability(self) -> None:
        with pytest.raises(ValueError):
            bo2_naive_outcome_probs(1.4)
