"""The feature builder is the one place features may be computed (spec section 6.4)."""

import math

from app.db.models.enums import SeriesFormat
from app.features.game_state import GameState, SeriesContext, TeamState
from app.features.live import FEATURE_ORDER, as_vector, build_live_features


def make_state(**overrides: object) -> GameState:
    defaults: dict[str, object] = {
        "match_id": 1,
        "minute": 20,
        "radiant": TeamState(
            score=15,
            net_worth=60000,
            towers_alive={"top": 3, "mid": 3, "bot": 3},
            barracks_alive={"top": 2, "mid": 2, "bot": 2},
            player_net_worths=(15000, 13000, 12000, 11000, 9000),
        ),
        "dire": TeamState(
            score=8,
            net_worth=48000,
            towers_alive={"top": 2, "mid": 1, "bot": 3},
            barracks_alive={"top": 2, "mid": 2, "bot": 2},
            player_net_worths=(12000, 11000, 9000, 8000, 8000),
        ),
        "gold_adv": 12000,
        "xp_adv": 9000,
    }
    defaults.update(overrides)
    return GameState(**defaults)  # type: ignore[arg-type]


def test_every_declared_feature_is_produced() -> None:
    features = build_live_features(make_state())
    assert set(features) == set(FEATURE_ORDER)
    assert len(as_vector(features)) == len(FEATURE_ORDER)


def test_vector_order_is_stable_regardless_of_dict_order() -> None:
    features = build_live_features(make_state())
    shuffled = dict(reversed(list(features.items())))
    assert as_vector(shuffled) == as_vector(features)


def test_building_and_time_features() -> None:
    features = build_live_features(make_state())
    assert features["tower_diff"] == 3.0  # 9 radiant towers vs 6 dire
    assert features["kill_diff"] == 7.0
    assert features["log_minute"] == math.log(21)
    # Normalising by time is what stops a minute-5 lead looking like a minute-40 lead.
    assert features["gold_adv_norm"] == 12000 / 25


def test_series_context_travels_with_game_number() -> None:
    """game_in_series alone is a trap - it must arrive with format and conditionality."""
    features = build_live_features(
        make_state(
            series=SeriesContext(
                series_format=SeriesFormat.BO2,
                game_in_series=2,
                is_conditional_game=False,
            )
        )
    )
    assert features["game_in_series"] == 2.0
    assert features["series_len"] == 2.0
    assert features["is_conditional_game"] == 0.0


class TestRoshanIsGone:
    """Dropped 27.08.2026 for the same reason xp_adv was: the serve path cannot supply
    these. `from_live_league_game` never passes `roshan_kills` or `aegis_holder_is_radiant`
    into the GameState at all, so both were constants in production while training saw real
    values. `roshan_respawn_in` the live scoreboard does supply, but STRATZ - now the only
    source of per-minute training data - carries no Roshan events whatsoever.
    """

    def test_the_three_features_are_not_in_the_vector(self) -> None:
        for name in ("roshan_kills", "aegis_holder", "roshan_respawn_in"):
            assert name not in FEATURE_ORDER

    def test_the_builder_ignores_the_raw_fields(self) -> None:
        """The GameState fields stay - they are raw data, and the OpenDota adapter still
        fills them. They simply must not reach the vector."""
        held = make_state(roshan_kills=3, aegis_holder_is_radiant=True, roshan_respawn_in=120)
        empty = make_state(roshan_kills=0, aegis_holder_is_radiant=None, roshan_respawn_in=None)
        assert build_live_features(held) == build_live_features(empty)

    def test_the_vector_is_25_long(self) -> None:
        """30 originally, 27 after Roshan left with no live source, 25 after `tier` and
        `is_lan` were measured to be constants in all 3974 featurised matches."""
        assert len(FEATURE_ORDER) == 25
        assert len(set(FEATURE_ORDER)) == 25
        assert set(build_live_features(make_state())) == set(FEATURE_ORDER)


class TestConstantsAreNotFeatures:
    """A column with one value in it teaches nothing, and one of these was worse than that.

    `tier` is fixed to 1 at inference by section 5.4 - the product only serves Tier 1 - so a
    tier that varied in training would be train/serve skew by construction, which is exactly
    what invariant 2 forbids and what already cost this project `xp_adv`.
    """

    def test_tier_is_not_in_the_vector(self) -> None:
        assert "tier" not in FEATURE_ORDER

    def test_is_lan_is_not_in_the_vector(self) -> None:
        """Knowable on both sides, but nothing has ever filled it: it measured 0.0 in every
        one of 3974 matches. It can come back when the sweep and the live path both supply
        it; a column of zeroes cannot."""
        assert "is_lan" not in FEATURE_ORDER

    def test_the_raw_fields_stay_on_the_state(self) -> None:
        """Same rule as Roshan: the state keeps what sources report, the vector takes only
        what a model may see."""
        state = make_state()
        assert hasattr(state, "tier")
        assert hasattr(state, "is_lan")

    def test_changing_them_no_longer_changes_the_vector(self) -> None:
        assert build_live_features(make_state(tier=1, is_lan=None)) == build_live_features(
            make_state(tier=3, is_lan=True)
        )
