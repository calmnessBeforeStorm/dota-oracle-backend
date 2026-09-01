"""What the service is allowed to say out loud (spec sections 7.4, 9.3).

The rule under test is not about model quality. It is about the one claim a live predictor
must never make: certainty. In Dota the losing side keeps its buyback and its ancient, and
every comeback that has ever happened started from a position some number called hopeless.
"""

import math

import pytest

from app.core.config import get_settings
from app.features.game_state import GameState, SeriesContext, TeamState
from app.features.live import build_live_features
from app.ml import predictor as predictor_module
from app.ml.predictor import (
    SERVING_BOUNDS,
    BaselinePredictor,
    _Bounded,
    bounded,
    get_predictor,
    reset_predictor,
)

LOW, HIGH = SERVING_BOUNDS


def state_with(gold_adv: int, minute: int) -> GameState:
    """An otherwise even game with one side ahead on gold."""
    side = TeamState(
        score=0,
        net_worth=50000,
        towers_alive={"top": 3, "mid": 3, "bot": 3},
        barracks_alive={"top": 2, "mid": 2, "bot": 2},
        player_net_worths=(10000,) * 5,
    )
    return GameState(
        match_id=1,
        minute=minute,
        radiant=side,
        dire=side,
        gold_adv=gold_adv,
        xp_adv=0,
        series=SeriesContext(),
    )


class Certain:
    """A predictor with no doubts, which is what an unclamped logistic becomes."""

    version = "certain-1.0"

    def __init__(self, p: float) -> None:
        self.p = p

    def predict_proba_radiant(self, features: dict[str, float]) -> float:
        return self.p


class TestBounds:
    def test_certainty_is_pulled_back(self) -> None:
        assert bounded(1.0) == HIGH
        assert bounded(0.0) == LOW

    def test_ordinary_probabilities_pass_through(self) -> None:
        for p in (0.5, 0.32, 0.87):
            assert bounded(p) == p

    def test_the_bounds_themselves_are_reachable(self) -> None:
        assert bounded(LOW) == LOW
        assert bounded(HIGH) == HIGH

    def test_a_confident_miss_becomes_survivable(self) -> None:
        """The measured damage this exists to stop. The baseline clamps its logit at +-20,
        so being wrong there costs 20 nats - one comeback then decides the log loss of a
        whole minute bucket, which is what happened to 30+ on the accuracy dashboard."""
        unbounded = -math.log(1.0 / (1.0 + math.exp(20.0)))
        clamped = -math.log(1.0 - bounded(1.0))

        assert unbounded > 19
        assert clamped < 5


class TestWrapping:
    def test_it_wraps_whatever_is_inside(self) -> None:
        assert _Bounded(Certain(1.0)).predict_proba_radiant({}) == HIGH

    def test_the_version_is_the_wrapped_model_not_the_wrapper(self) -> None:
        """The dashboard groups by this string. A wrapper name here would split one model's
        history in two the day the guard was added."""
        assert _Bounded(Certain(0.5)).version == "certain-1.0"

    def test_the_served_predictor_is_bounded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The guard belongs to serving, not to the placeholder: phase 4 swaps the booster
        in, and a rule written into the baseline would have left with it.

        The active model is pinned off rather than taken from the environment. This test used
        to read whatever `.env` said and passed only because that had always been empty; the
        day a model was promoted it started asking a boosted tree to extrapolate to a 5000
        gold lead, which trees do not do, and failed for a reason that had nothing to do with
        the guard it is about.
        """
        # pydantic-settings, so `model_copy`; `dataclasses.replace` refuses it.
        without_model = get_settings().model_copy(update={"active_model_version": None})
        monkeypatch.setattr(predictor_module, "get_settings", lambda: without_model)
        reset_predictor()
        try:
            served = get_predictor()
            # A lead far past anything a real match produces.
            extreme = served.predict_proba_radiant({"gold_adv_norm": 5000.0, "minute": 60.0})
        finally:
            reset_predictor()

        assert isinstance(served, _Bounded)
        assert extreme == HIGH

    def test_whatever_is_active_comes_back_bounded(self) -> None:
        """Structural, so it holds for the configured model as well as for the baseline."""
        reset_predictor()
        try:
            assert isinstance(get_predictor(), _Bounded)
        finally:
            reset_predictor()

    def test_the_baseline_itself_still_saturates(self) -> None:
        """Kept honest on purpose: the baseline is spec section 7.3's benchmark and must not
        quietly become a different model because serving grew a guard."""
        raw = BaselinePredictor().predict_proba_radiant({"gold_adv_norm": 5000.0, "minute": 60.0})

        assert raw > HIGH


class TestTheBaselineReadsTheRightFeature:
    """It used to read raw `gold_adv` while `gold_adv_norm` sat beside it in the same dict.

    The cost was not subtle. On a 140-match holdout the old formula scored 1.299 in the
    30+ minute bucket - worse than the 0.693 of a coin flip - because its time term had the
    wrong sign and made a late lead count for more, where the data says it counts for less.
    """

    def test_a_lead_is_worth_less_the_later_it_appears(self) -> None:
        """Measured: above 8k wins 98.0% of the time in minutes 10-20 and 82.9% after 40."""
        early = build_live_features(state_with(gold_adv=8000, minute=15))
        late = build_live_features(state_with(gold_adv=8000, minute=45))
        model = BaselinePredictor()

        assert model.predict_proba_radiant(early) > model.predict_proba_radiant(late)

    def test_every_feature_it_reads_is_one_the_builder_produces(self) -> None:
        """The failure mode this guards is silent: an absent key defaults to zero, so a
        renamed feature does not raise, it just makes the model predict a coin flip forever."""
        produced = build_live_features(state_with(gold_adv=0, minute=10))

        assert "gold_adv_norm" in produced
        assert "minute" in produced

    def test_an_even_game_is_close_to_even(self) -> None:
        features = build_live_features(state_with(gold_adv=0, minute=20))

        assert 0.4 < BaselinePredictor().predict_proba_radiant(features) < 0.6
