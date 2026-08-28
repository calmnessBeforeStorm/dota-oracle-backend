"""The one and only feature builder (spec sections 6.1, 6.4).

Both the offline pipeline and the live service call `build_live_features(GameState)`.
Nothing else is allowed to compute a feature.

**A feature only exists here if both sources can supply it.** That rule cost us the XP
advantage, which is worth recording because it is tempting to add back.

OpenDota gives `radiant_xp_adv` per minute, exactly. The live scoreboard gives neither
cumulative XP nor a way to recover it: measured over 400 stored matches, reconstructing a
player's level from XP through an empirically derived threshold table lands within one level
only 53% of the time, and the resulting `xp_adv` sits 25.6% off the true value at the median.
A feature that is exact in training and 25% wrong in production is precisely the train/serve
skew section 6.4 warns about - and it would show up as a model that works in the notebook
and disappoints in the live loop, with nothing in the metrics to explain it.

Gold advantage carries most of the same information and both sides supply it exactly, so
the XP advantage stays on `GameState` as raw data and out of the feature vector.

Roshan went the same way on 27.08.2026, and the first two for a sharper reason than XP:
`from_live_league_game` never passes `roshan_kills` or `aegis_holder_is_radiant` into the
GameState at all, so both had been constants in production while training saw real values -
a skew that had simply gone unnoticed. `roshan_respawn_in` the live scoreboard does supply,
but STRATZ, now the only source of per-minute training data, carries no Roshan events
whatsoever: `roshanEvents` comes back empty and its chat log has no such entries, measured
over 60 matches. All three stay on `GameState` as raw data and out of the feature vector.
"""

import math
from collections.abc import Sequence

from app.features.game_state import GameState

#: `tier` and `is_lan` are deliberately absent, and each for its own reason.
#:
#: `tier` is a trap rather than a weak feature. Section 5.4 fixes it to 1 at inference,
#: because the product only serves Tier 1 - so a tier that varies in training and is constant
#: in production is train/serve skew by construction, which is the mistake that already cost
#: this project `xp_adv` (invariant 2). Measured before removal: it was the constant 1.0 in
#: all 3974 featurised matches, so nothing was lost by dropping it.
#:
#: `is_lan` is knowable on both sides - `leagues.is_lan` is there and the poller resolves the
#: league - but nothing has ever filled it. It was the constant 0.0 in all 3974 matches. It
#: can come back when both the sweep and the live path supply it; a column of zeroes cannot.
FEATURE_ORDER: tuple[str, ...] = (
    # time
    "minute",
    "log_minute",
    # economy
    "gold_adv",
    "gold_adv_norm",
    # buildings
    "tower_diff",
    "barracks_diff",
    "radiant_towers",
    "dire_towers",
    # fighting
    "kill_diff",
    "net_worth_diff",
    "radiant_nw_spread",
    "dire_nw_spread",
    # context
    "game_in_series",
    "is_conditional_game",
    "series_len",
    "series_wins_diff",
    # priors and pre-match context (spec section 6.2)
    "prematch_prior",
    "skill_diff",
    "skill_sigma_sum",
    "established_diff",
    "form_diff",
    "h2h_advantage",
    "draft_advantage",
    "rest_days_diff",
    "maps_last_24h_diff",
)

#: The subset supplied by the pre-match sweep rather than by the game state.
PREMATCH_FEATURE_NAMES: tuple[str, ...] = (
    "skill_diff",
    "skill_sigma_sum",
    "established_diff",
    "form_diff",
    "h2h_advantage",
    "draft_advantage",
    "rest_days_diff",
    "maps_last_24h_diff",
)


#: Softens the division near minute zero, where the raw ratio would explode.
#:
#: The normalisation is not cosmetic. Measured over 30539 snapshots, the same absolute lead
#: is worth steadily less as the game goes on: a lead above 8k wins 98.0% of the time in
#: minutes 10-20 and 82.9% after minute 40, and a deficit below -8k recovers 6.1% of the time
#: early against 13.5% late. A model given raw gold has one weight to express both.
GOLD_NORM_OFFSET = 5.0


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
        # Normalising by time keeps an early 2k lead from looking like a late 2k lead.
        "gold_adv_norm": state.gold_adv / (minute + GOLD_NORM_OFFSET),
        "tower_diff": float(state.radiant.tower_count - state.dire.tower_count),
        "barracks_diff": float(state.radiant.barracks_count - state.dire.barracks_count),
        "radiant_towers": float(state.radiant.tower_count),
        "dire_towers": float(state.dire.tower_count),
        "kill_diff": float(state.radiant.score - state.dire.score),
        "net_worth_diff": float(state.radiant.net_worth - state.dire.net_worth),
        "radiant_nw_spread": _spread(state.radiant.player_net_worths),
        "dire_nw_spread": _spread(state.dire.player_net_worths),
        "game_in_series": float(state.series.game_in_series),
        "is_conditional_game": float(state.series.is_conditional_game),
        "series_len": float(state.series.series_format.max_games),
        "series_wins_diff": float(state.series.radiant_series_wins - state.series.dire_series_wins),
        "prematch_prior": 0.5 if state.prematch_prior is None else state.prematch_prior,
    }

    # Zero is the neutral value for every one of these: they are differences between the
    # two sides, so "we know nothing" and "the sides are equal" coincide. That is not true
    # of the state features above, which is why only these default.
    for name in PREMATCH_FEATURE_NAMES:
        features[name] = float(state.prematch.get(name, 0.0))
    missing = set(FEATURE_ORDER) - features.keys()
    if missing:
        raise RuntimeError(f"feature builder is out of sync with FEATURE_ORDER: {sorted(missing)}")
    return features


def as_vector(features: dict[str, float]) -> list[float]:
    """Ordered vector for the model. Order is fixed by FEATURE_ORDER, never by dict order."""
    return [features[name] for name in FEATURE_ORDER]
