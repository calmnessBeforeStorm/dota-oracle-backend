"""Adapter: OpenDota parsed match -> GameState at a given minute (spec sections 2.2/A2, 6.4).

This is the train-time path: it unrolls the per-minute series of a finished, parsed match
into one GameState per minute. Its output must agree with the live adapter for the same
match - see tests/features/test_train_serve_parity.py.

Everything here reads only what was knowable at the minute being described. The payload is
full of end-of-match summaries that would be trivial to reach for and would leak the result
straight into the training data (spec section 12).
"""

from collections.abc import Mapping
from typing import Any

from app.features.buildings import BuildingState, state_at
from app.features.game_state import GameState, SeriesContext, TeamState

#: Radiant occupies player slots 0-4, dire 128-132.
DIRE_SLOT_THRESHOLD = 128

ROSHAN_KILL = "CHAT_MESSAGE_ROSHAN_KILL"
AEGIS_PICKUP = ("CHAT_MESSAGE_AEGIS", "CHAT_MESSAGE_AEGIS_STOLEN")
#: Valve numbers radiant 2 and dire 3 in the objectives log.
RADIANT_TEAM_NUMBER = 2

#: Aegis expires five minutes after it is picked up.
AEGIS_DURATION = 5 * 60
#: Roshan respawns 8-11 minutes after dying; the midpoint is the honest estimate, since the
#: exact value is not knowable from the log.
ROSHAN_RESPAWN = int(9.5 * 60)


def is_parsed(match: dict[str, Any]) -> bool:
    """`version` is null for unparsed matches - no per-minute series available."""
    return match.get("version") is not None


def _at(series: list[int] | None, minute: int) -> int:
    if not series:
        return 0
    return int(series[min(minute, len(series) - 1)])


def _kills_before(player: dict[str, Any], minute: int) -> int:
    return len([k for k in (player.get("kills_log") or []) if k.get("time", 0) <= minute * 60])


def _team_state_at(
    match: dict[str, Any], minute: int, radiant: bool, buildings: BuildingState
) -> TeamState:
    players = [
        p
        for p in match.get("players", []) or []
        if (p.get("player_slot", 0) < DIRE_SLOT_THRESHOLD) is radiant
    ]
    net_worths = tuple(_at(p.get("gold_t"), minute) for p in players)
    return TeamState(
        score=sum(_kills_before(p, minute) for p in players),
        net_worth=sum(net_worths),
        towers_alive=buildings.towers,
        barracks_alive=buildings.barracks,
        ancient_alive=buildings.ancient_alive,
        player_net_worths=net_worths,
    )


def _roshan_at(
    objectives: list[dict[str, Any]], minute: int
) -> tuple[int, bool | None, int | None]:
    """Roshan kills so far, who holds the aegis, and seconds until the next respawn."""
    cutoff = (minute + 1) * 60
    kills = 0
    last_kill_at: int | None = None
    aegis_holder: bool | None = None
    aegis_taken_at: int | None = None

    for event in objectives:
        time = int(event.get("time", 0))
        if time >= cutoff:
            continue
        kind = event.get("type")
        if kind == ROSHAN_KILL:
            kills += 1
            last_kill_at = time
        elif kind in AEGIS_PICKUP:
            aegis_holder = event.get("team") == RADIANT_TEAM_NUMBER
            aegis_taken_at = time

    now = min(cutoff, (minute + 1) * 60)
    if aegis_taken_at is not None and now - aegis_taken_at > AEGIS_DURATION:
        aegis_holder = None

    respawn_in: int | None = None
    if last_kill_at is not None:
        remaining = last_kill_at + ROSHAN_RESPAWN - now
        respawn_in = remaining if remaining > 0 else None

    return kills, aegis_holder, respawn_in


def _picks(match: dict[str, Any], radiant: bool) -> tuple[int, ...]:
    """Hero ids of one side. Drafted before the horn, so known at every minute."""
    return tuple(
        int(p["hero_id"])
        for p in match.get("players", []) or []
        if p.get("hero_id") and (p.get("player_slot", 0) < DIRE_SLOT_THRESHOLD) is radiant
    )


def snapshot_at(
    match: dict[str, Any],
    minute: int,
    series: SeriesContext | None = None,
    prematch_prior: float | None = None,
    prematch: Mapping[str, float] | None = None,
) -> GameState:
    """One training snapshot. Only information available at `minute` may be read."""
    objectives = match.get("objectives") or []
    roshan_kills, aegis_radiant, respawn_in = _roshan_at(objectives, minute)

    return GameState(
        match_id=int(match["match_id"]),
        minute=minute,
        # Replayed from the objectives log, never read off tower_status_*: that field is the
        # state at the end of the match and would leak the result (spec section 12).
        radiant=_team_state_at(match, minute, True, state_at(objectives, minute, radiant=True)),
        dire=_team_state_at(match, minute, False, state_at(objectives, minute, radiant=False)),
        gold_adv=_at(match.get("radiant_gold_adv"), minute),
        xp_adv=_at(match.get("radiant_xp_adv"), minute),
        roshan_kills=roshan_kills,
        aegis_holder_is_radiant=aegis_radiant,
        roshan_respawn_in=respawn_in,
        radiant_picks=_picks(match, radiant=True),
        dire_picks=_picks(match, radiant=False),
        series=series or SeriesContext(),
        prematch=prematch or {},
        prematch_prior=prematch_prior,
    )


def iter_snapshots(
    match: dict[str, Any],
    series: SeriesContext | None = None,
    min_minute: int = 0,
    prematch: Mapping[str, float] | None = None,
    prematch_prior: float | None = None,
) -> list[GameState]:
    """Unroll a parsed match into one snapshot per minute.

    Snapshots of one match are heavily correlated: any split must be by `match_id`,
    never by row (spec section 5.1).
    """
    if not is_parsed(match):
        raise ValueError(f"match {match.get('match_id')} is not parsed, no per-minute series")
    last_minute = int(match.get("duration", 0)) // 60
    return [
        snapshot_at(match, m, series, prematch_prior, prematch)
        for m in range(min_minute, last_minute + 1)
    ]
