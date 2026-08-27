"""Building state, from either source (spec sections 6.1, 6.4).

Buildings are the second-strongest signal after gold, and the easiest to get catastrophically
wrong. The match payload carries `tower_status_radiant` and `barracks_status_radiant`, which
look like exactly what a snapshot needs and are in fact the state at the *end* of the match:
putting them in a minute-15 row leaks the result into the features.

The honest route is to start from a full base and replay the `building_kill` events up to the
minute in question. That is what `state_at` does.

Live data needs no replay - Steam states the current bitmask directly - and, usefully, it is
the same bitmask layout OpenDota uses for the final state. One decoder serves both, which is
one less place for train and serve to drift apart.
"""

import re
from dataclasses import dataclass
from typing import Any

LANES = ("top", "mid", "bot")
#: The two towers guarding the ancient. They have no lane, so they get their own bucket.
BASE = "base"

FULL_TOWERS: dict[str, int] = {"top": 3, "mid": 3, "bot": 3, BASE: 2}
FULL_BARRACKS: dict[str, int] = {"top": 2, "mid": 2, "bot": 2}

#: `npc_dota_goodguys_tower1_top`, `npc_dota_badguys_melee_rax_mid`, `npc_dota_goodguys_fort`.
#: goodguys is Radiant, badguys is Dire.
_BUILDING_KEY = re.compile(
    r"npc_dota_(?P<side>goodguys|badguys)_"
    r"(?P<what>tower[1-4]|melee_rax|range_rax|fort)"
    r"(?:_(?P<lane>top|mid|bot))?$"
)


@dataclass(frozen=True)
class BuildingState:
    towers: dict[str, int]
    barracks: dict[str, int]
    ancient_alive: bool = True

    @property
    def tower_count(self) -> int:
        return sum(self.towers.values())

    @property
    def barracks_count(self) -> int:
        return sum(self.barracks.values())


def full_base() -> BuildingState:
    return BuildingState(towers=dict(FULL_TOWERS), barracks=dict(FULL_BARRACKS))


@dataclass(frozen=True)
class BuildingKill:
    """One destroyed building, as named in the objectives log."""

    is_radiant: bool
    kind: str  # "tower" | "barracks" | "ancient"
    lane: str  # one of LANES, or BASE for the ancient towers


def parse_building_key(key: str) -> BuildingKill | None:
    """Read a `building_kill` key. Returns None for anything unrecognised.

    Unrecognised is not an error worth raising: the log is Valve's, event names change, and
    an unknown building simply leaves the state as it was rather than corrupting it.
    """
    match = _BUILDING_KEY.match(key.strip())
    if not match:
        return None

    is_radiant = match.group("side") == "goodguys"
    what = match.group("what")
    lane = match.group("lane")

    if what == "fort":
        return BuildingKill(is_radiant, "ancient", BASE)
    if what == "tower4":
        # Both ancient towers share this name, so a side can lose it twice.
        return BuildingKill(is_radiant, "tower", BASE)
    if what.startswith("tower"):
        return BuildingKill(is_radiant, "tower", lane) if lane else None
    return BuildingKill(is_radiant, "barracks", lane) if lane else None


def apply_kill(state: BuildingState, kill: BuildingKill) -> BuildingState:
    """State after one building falls. Never goes below zero."""
    towers = dict(state.towers)
    barracks = dict(state.barracks)
    ancient = state.ancient_alive

    if kill.kind == "tower":
        towers[kill.lane] = max(0, towers.get(kill.lane, 0) - 1)
    elif kill.kind == "barracks":
        barracks[kill.lane] = max(0, barracks.get(kill.lane, 0) - 1)
    else:
        ancient = False

    return BuildingState(towers=towers, barracks=barracks, ancient_alive=ancient)


def state_at(objectives: list[dict[str, Any]], minute: int, radiant: bool) -> BuildingState:
    """Building state of one side at the end of the given minute.

    Only events at or before that minute are applied - that is the whole point. Times are
    seconds from the horn and can be negative, which is fine: a pre-horn event belongs to
    minute zero.
    """
    state = full_base()
    cutoff = (minute + 1) * 60

    for event in objectives:
        if event.get("type") != "building_kill":
            continue
        if int(event.get("time", 0)) >= cutoff:
            continue
        kill = parse_building_key(str(event.get("key") or ""))
        if kill is not None and kill.is_radiant is radiant:
            state = apply_kill(state, kill)

    return state


#: Bit layout Valve uses for `tower_status_*`, shared by OpenDota's final state and Steam's
#: live `tower_state`. Bit set means the building still stands.
_TOWER_BITS: tuple[tuple[int, str], ...] = (
    (0, "top"), (1, "top"), (2, "top"),
    (3, "mid"), (4, "mid"), (5, "mid"),
    (6, "bot"), (7, "bot"), (8, "bot"),
    (9, BASE), (10, BASE),
)  # fmt: skip

#: `barracks_status_*`: melee then ranged, per lane.
_BARRACKS_BITS: tuple[tuple[int, str], ...] = (
    (0, "top"), (1, "top"),
    (2, "mid"), (3, "mid"),
    (4, "bot"), (5, "bot"),
)  # fmt: skip


def decode_bitmasks(tower_status: int, barracks_status: int) -> BuildingState:
    """Building state from Valve's bitmasks.

    Safe for live data, where the mask *is* the current state. Never use it on a finished
    match to describe an earlier minute: there the mask describes the end of the game.
    """
    towers = dict.fromkeys([*LANES, BASE], 0)
    for bit, lane in _TOWER_BITS:
        if tower_status & (1 << bit):
            towers[lane] += 1

    barracks = dict.fromkeys(LANES, 0)
    for bit, lane in _BARRACKS_BITS:
        if barracks_status & (1 << bit):
            barracks[lane] += 1

    return BuildingState(towers=towers, barracks=barracks)
