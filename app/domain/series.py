"""Series-level rules (spec section 5.5).

Everything Bo2-related lives here. The live and pre-match models are untouched by it:
their unit is a map, and a map always has a winner. Only the series layer - the `series`
table, the UI score, standings and the v2 series model - has to know about draws.
"""

from dataclasses import dataclass

from app.db.models.enums import SeriesFormat

# A Bo2 game 2 is always played, a Bo3 game 3 only at 1-1. That difference is what
# `is_conditional_game` encodes.
_UNCONDITIONAL_GAMES: dict[SeriesFormat, int] = {
    SeriesFormat.BO1: 1,
    SeriesFormat.BO2: 2,
    SeriesFormat.BO3: 2,
    SeriesFormat.BO5: 3,
}


def resolve_format(
    stage_default: SeriesFormat | None,
    series_override: SeriesFormat | None = None,
    valve_series_type: int | None = None,
) -> SeriesFormat:
    """Resolve the format of a series, in the order the spec mandates.

    1. An explicit override on the series itself (replay, tiebreaker).
    2. The Liquipedia stage default - the source of truth.
    3. Valve `series_type`, a hint only: Bo2 is not representable there (it usually
       arrives as 0), so it can never produce BO2 and is used solely as a last resort.
    """
    if series_override is not None:
        return series_override
    if stage_default is not None:
        return stage_default
    return {0: SeriesFormat.BO1, 1: SeriesFormat.BO3, 2: SeriesFormat.BO5}.get(
        valve_series_type if valve_series_type is not None else -1, SeriesFormat.BO1
    )


def is_conditional_game(fmt: SeriesFormat, game_in_series: int) -> bool:
    """True when this map was played only because of the series score.

    Feed this alongside `game_in_series`, never `game_in_series` alone: in Bo3 the third
    map only happens at 1-1, so the sample of third maps is skewed toward evenly matched
    opponents, and the model learns the format artifact instead of a real effect.
    """
    return game_in_series > _UNCONDITIONAL_GAMES[fmt]


@dataclass(frozen=True)
class SeriesOutcome:
    score_a: int
    score_b: int
    winner_is_a: bool | None  # None = draw
    is_draw: bool
    is_decided: bool


def series_outcome(fmt: SeriesFormat, score_a: int, score_b: int) -> SeriesOutcome:
    """Resolve a series score into an outcome. Only Bo2 can end in a draw."""
    played = score_a + score_b
    if fmt is SeriesFormat.BO2:
        if played < 2:
            return SeriesOutcome(score_a, score_b, None, is_draw=False, is_decided=False)
        if score_a == score_b:
            return SeriesOutcome(score_a, score_b, None, is_draw=True, is_decided=True)
        return SeriesOutcome(score_a, score_b, score_a > score_b, is_draw=False, is_decided=True)

    needed = fmt.max_games // 2 + 1
    if score_a >= needed:
        return SeriesOutcome(score_a, score_b, True, is_draw=False, is_decided=True)
    if score_b >= needed:
        return SeriesOutcome(score_a, score_b, False, is_draw=False, is_decided=True)
    return SeriesOutcome(score_a, score_b, None, is_draw=False, is_decided=False)


def bo2_naive_outcome_probs(p_a: float) -> dict[str, float]:
    """Naive Bo2 outcome distribution from a per-map probability: p^2 / 2p(1-p) / (1-p)^2.

    Starting point only. Map independence does NOT hold - the second map correlates with
    the first (draft adjustment, momentum, revealed strategy), and in practice the real
    share of 1-1 runs higher than 2p(1-p). The correction has to be estimated empirically
    (v2, spec section 5.5), not assumed.
    """
    if not 0.0 <= p_a <= 1.0:
        raise ValueError(f"p_a must be in [0, 1], got {p_a}")
    return {"2-0": p_a * p_a, "1-1": 2 * p_a * (1 - p_a), "0-2": (1 - p_a) * (1 - p_a)}
