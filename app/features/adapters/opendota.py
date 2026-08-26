"""Adapter: OpenDota parsed match -> GameState at a given minute (spec sections 2.2/A2, 6.4).

This is the train-time path: it unrolls the per-minute series of a finished, parsed match
into one GameState per minute. Its output must agree with the Steam adapter for the same
match - see tests/features/test_train_serve_parity.py.
"""

from typing import Any

from app.features.game_state import GameState, SeriesContext, TeamState

LANES = ("top", "mid", "bot")
_FULL_TOWERS = {"top": 3, "mid": 3, "bot": 3}
_FULL_BARRACKS = {"top": 2, "mid": 2, "bot": 2}


def is_parsed(match: dict[str, Any]) -> bool:
    """`version` is null for unparsed matches - no per-minute series available."""
    return match.get("version") is not None


def _at(series: list[int] | None, minute: int) -> int:
    if not series:
        return 0
    return int(series[min(minute, len(series) - 1)])


def _team_state_at(match: dict[str, Any], minute: int, radiant: bool) -> TeamState:
    players = [
        p for p in match.get("players", []) or [] if (p.get("player_slot", 0) < 128) is radiant
    ]
    kills = sum(len(_kills_before(p, minute)) for p in players)
    net_worths = tuple(_at(p.get("gold_t"), minute) for p in players)
    return TeamState(
        score=kills,
        net_worth=sum(net_worths),
        # TODO(phase-3): replay the objectives log to get living buildings per minute.
        # tower_status_* on the match is the FINAL state - using it here would leak the future.
        towers_alive=dict(_FULL_TOWERS),
        barracks_alive=dict(_FULL_BARRACKS),
        player_net_worths=net_worths,
    )


def _kills_before(player: dict[str, Any], minute: int) -> list[Any]:
    return [k for k in (player.get("kills_log") or []) if k.get("time", 0) <= minute * 60]


def snapshot_at(
    match: dict[str, Any],
    minute: int,
    series: SeriesContext | None = None,
    prematch_prior: float | None = None,
) -> GameState:
    """One training snapshot. Only information available at `minute` may be read."""
    return GameState(
        match_id=int(match["match_id"]),
        minute=minute,
        radiant=_team_state_at(match, minute, radiant=True),
        dire=_team_state_at(match, minute, radiant=False),
        gold_adv=_at(match.get("radiant_gold_adv"), minute),
        xp_adv=_at(match.get("radiant_xp_adv"), minute),
        # TODO(phase-3): roshan + aegis from objectives[]
        series=series or SeriesContext(),
        prematch_prior=prematch_prior,
    )


def iter_snapshots(
    match: dict[str, Any],
    series: SeriesContext | None = None,
    min_minute: int = 0,
) -> list[GameState]:
    """Unroll a parsed match into one snapshot per minute.

    Snapshots of one match are heavily correlated: any split must be by `match_id`,
    never by row (spec section 5.1).
    """
    if not is_parsed(match):
        raise ValueError(f"match {match.get('match_id')} is not parsed, no per-minute series")
    last_minute = int(match.get("duration", 0)) // 60
    return [snapshot_at(match, m, series) for m in range(min_minute, last_minute + 1)]
