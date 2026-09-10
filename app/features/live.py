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
from collections.abc import Mapping, Sequence
from functools import cache

from app.features.game_state import GameState, TeamState

#: `tier` is deliberately absent, and it is a trap rather than a weak feature. Section 5.4
#: fixes it to 1 at inference, because the product only serves Tier 1 - so a tier that varies
#: in training and is constant in production is train/serve skew by construction, which is
#: the mistake that already cost this project `xp_adv` (invariant 2). Measured before
#: removal: it was the constant 1.0 in all 3974 featurised matches.
#:
#: `is_lan` left with it for a different reason - nothing had ever filled it - and came back
#: once both sides did. Measured across the featurised set: 692 online, 510 LAN, 3201 unknown.
#: Real variation where it is known, which is why it travels with `is_lan_known` rather than
#: letting three quarters of the data pass for "online".
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
    "is_lan",
    "is_lan_known",
    "series_format_known",
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

#: Everything the pre-match sweep owns, prior included.
PREMATCH_BLOCK: tuple[str, ...] = (*PREMATCH_FEATURE_NAMES, "prematch_prior")

#: The features a model may consume today: those the live path fills from the payload.
#:
#: The builder still computes the pre-match block, because `featurize` stores it and the
#: poller will eventually pass it. But `from_live_league_game` does not pass it, so at
#: serving time every one of those features is the default below - the same number in every
#: match - while training saw real values. Measured 10.09.2026 on `lgbm-20260901-102407`:
#: 1575 of its 2190 splits stood on that block, and `prematch_prior` arrived as 0.5, a value
#: that appears in exactly 0 of 242295 training rows. Worse, `skill_sigma_sum` arrived as
#: 0.0, below its training minimum of 1.2356 - a value the model had never seen at all.
#:
#: Training on a signal the serving path cannot supply is train/serve skew whatever the
#: number substituted, so a model is trained on this set until the poller supplies the rest.
SERVED_FEATURES: tuple[str, ...] = tuple(n for n in FEATURE_ORDER if n not in PREMATCH_BLOCK)


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
        # Paired with its own "do we know" flag, and so is the series format. The vector is
        # floats, so an unknown has to be written as some number; without the companion that
        # number is indistinguishable from a real one, and the model learns our labelling
        # coverage instead of the game.
        "is_lan": 0.0 if state.is_lan is None else float(state.is_lan),
        "is_lan_known": float(state.is_lan is not None),
        "series_format_known": float(state.series.format_known),
        "game_in_series": float(state.series.game_in_series),
        "is_conditional_game": float(state.series.is_conditional_game),
        "series_len": float(state.series.series_format.max_games),
        "series_wins_diff": float(state.series.radiant_series_wins - state.series.dire_series_wins),
        "prematch_prior": 0.5 if state.prematch_prior is None else state.prematch_prior,
    }

    # Zero reads as neutral for most of these - they are differences between the two sides,
    # so "we know nothing" and "the sides are equal" coincide. It is NOT true of
    # `skill_sigma_sum`, which is a sum of two TrueSkill sigmas and never reaches zero in
    # training (minimum 1.2356 over 242295 rows): zero there is a value the model has never
    # seen. Either way a default is only honest while nothing consumes it - see
    # `SERVED_FEATURES`.
    for name in PREMATCH_FEATURE_NAMES:
        features[name] = float(state.prematch.get(name, 0.0))
    missing = set(FEATURE_ORDER) - features.keys()
    if missing:
        raise RuntimeError(f"feature builder is out of sync with FEATURE_ORDER: {sorted(missing)}")
    return features


@cache
def live_defaults() -> Mapping[str, float]:
    """What the live path substitutes for the features it does not supply.

    Derived by running the builder over a state shaped the way the poller shapes one -
    `prematch` empty, `prematch_prior` absent - instead of restating the constants. Restating
    them is exactly how the comment above those defaults came to describe `skill_sigma_sum`
    as a difference between the two sides, which it is not. A copy drifts; this cannot.
    """
    blank = GameState(
        match_id=0,
        minute=1,
        radiant=TeamState(),
        dire=TeamState(),
        gold_adv=0,
        xp_adv=0,
    )
    built = build_live_features(blank)
    return {name: built[name] for name in PREMATCH_BLOCK}


def serving_view(features: Mapping[str, float]) -> dict[str, float]:
    """A training row as the serving path would actually produce it.

    The gate scores the holdout through this, so what a card reports is what production
    would run. Scoring the stored row instead measures a model that only exists in the
    training set: `match_snapshots` carries a real pre-match block, and the live path
    carries none of it.
    """
    return {**features, **live_defaults()}


def as_vector(features: dict[str, float], order: Sequence[str] = FEATURE_ORDER) -> list[float]:
    """Ordered vector for the model. Order comes from the caller, never from dict order.

    `order` is the feature set the model was trained on, which is not always the full
    builder output: a model trained on `SERVED_FEATURES` reads 19 of the 28 keys here.
    Passing the model's own list - `ModelCard.feature_order` at serving time - is what keeps
    a booster from being fed a vector in a shape it was never fitted on.
    """
    return [features[name] for name in order]
