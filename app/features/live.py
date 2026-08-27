"""The one and only feature builder (spec sections 6.1, 6.4).

Both the offline pipeline and the live service call `build_live_features(GameState)`.
Nothing else is allowed to compute a feature.
"""

import math
from collections.abc import Sequence

from app.features.game_state import GameState

FEATURE_ORDER: tuple[str, ...] = (
    # time
    "minute",
    "log_minute",
    # economy
    "gold_adv",
    "xp_adv",
    "gold_adv_norm",
    "xp_adv_norm",
    # buildings
    "tower_diff",
    "barracks_diff",
    "radiant_towers",
    "dire_towers",
    # roshan
    "roshan_kills",
    "aegis_holder",
    "roshan_respawn_in",
    # fighting
    "kill_diff",
    "net_worth_diff",
    "radiant_nw_spread",
    "dire_nw_spread",
    # context
    "tier",
    "is_lan",
    "game_in_series",
    "is_conditional_game",
    "series_len",
    "series_wins_diff",
    # priors
    "prematch_prior",
)


def _spread(values: Sequence[int]) -> float:
    """Net worth spread inside a team - a rough proxy for how concentrated the farm is."""
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / len(values))


def build_live_features(state: GameState) -> dict[str, float]:
    """Turn a GameState into the flat feature dict the live model consumes.

    Values are plain floats so the same dict can be logged to `predictions.features`
    and replayed later.
    """
    minute = state.minute
    features: dict[str, float] = {
        "minute": float(minute),
        "log_minute": math.log(minute + 1),
        "gold_adv": float(state.gold_adv),
        "xp_adv": float(state.xp_adv),
        # Normalising by time keeps an early 2k lead from looking like a late 2k lead.
        "gold_adv_norm": state.gold_adv / (minute + 5),
        "xp_adv_norm": state.xp_adv / (minute + 5),
        "tower_diff": float(state.radiant.tower_count - state.dire.tower_count),
        "barracks_diff": float(state.radiant.barracks_count - state.dire.barracks_count),
        "radiant_towers": float(state.radiant.tower_count),
        "dire_towers": float(state.dire.tower_count),
        "roshan_kills": float(state.roshan_kills),
        # Three states, not two: nobody holds it, dire holds it, radiant holds it. Mapping
        # "dire holds" and "nobody holds" both to zero would throw away half the signal,
        # and holding the aegis is worth a teamfight.
        "aegis_holder": (
            0.0
            if state.aegis_holder_is_radiant is None
            else (1.0 if state.aegis_holder_is_radiant else -1.0)
        ),
        "roshan_respawn_in": float(state.roshan_respawn_in or 0),
        "kill_diff": float(state.radiant.score - state.dire.score),
        "net_worth_diff": float(state.radiant.net_worth - state.dire.net_worth),
        "radiant_nw_spread": _spread(state.radiant.player_net_worths),
        "dire_nw_spread": _spread(state.dire.player_net_worths),
        "tier": float(state.tier),
        "is_lan": 0.0 if state.is_lan is None else float(state.is_lan),
        "game_in_series": float(state.series.game_in_series),
        "is_conditional_game": float(state.series.is_conditional_game),
        "series_len": float(state.series.series_format.max_games),
        "series_wins_diff": float(state.series.radiant_series_wins - state.series.dire_series_wins),
        "prematch_prior": 0.5 if state.prematch_prior is None else state.prematch_prior,
    }
    missing = set(FEATURE_ORDER) - features.keys()
    if missing:
        raise RuntimeError(f"feature builder is out of sync with FEATURE_ORDER: {sorted(missing)}")
    return features


def as_vector(features: dict[str, float]) -> list[float]:
    """Ordered vector for the model. Order is fixed by FEATURE_ORDER, never by dict order."""
    return [features[name] for name in FEATURE_ORDER]
