"""STRATZ against OpenDota on the same matches (spec section 6.4).

What this suite is really guarding is the npc id table: a wrong entry shows up immediately
as a building that falls in the wrong place or not at all.

Three properties of the comparison were measured on the fixtures and shape every assertion
below, so they are written down rather than discovered again:

  - **The clocks differ by zero or one second.** Every STRATZ event time is equal to or one
    second below OpenDota's for the same event. That is enough to move a single event across
    a minute boundary, so per-minute counts are compared with a tolerance of one rather than
    for equality. The event *sequence* is immune to it and is compared exactly.
  - **The two kill logs differ by at most one event per side.** OpenDota's `kills_log` is
    missing one kill on match 8944612322 that STRATZ records. Neither source is wrong often
    enough to call the other authoritative.
  - **The gold series are only loosely related, by design.** OpenDota reports earned gold,
    STRATZ reports net worth. Pearson correlation across the fixtures ranges from 0.66 to
    0.998 - the low end being a 69-minute game full of buybacks. Nothing tighter than "the
    same side is ahead at the end" is asserted, and nothing tighter should be: making these
    two agree would mean breaking one of them.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from app.features.adapters import opendota, stratz
from app.features.buildings import parse_building_key, parse_npc_id

OPENDOTA = Path(__file__).resolve().parent.parent / "fixtures" / "opendota"
STRATZ = Path(__file__).resolve().parent.parent / "fixtures" / "stratz"
PAIRS = sorted(
    (o, STRATZ / o.name) for o in OPENDOTA.glob("match_*.json") if (STRATZ / o.name).exists()
)

#: One second of clock skew can carry at most one event over a minute boundary, and the two
#: kill logs differ by at most one event per side. Both measured, not assumed.
TOLERANCE = 1


@pytest.fixture(params=PAIRS, ids=lambda p: p[0].stem)
def pair(request: pytest.FixtureRequest) -> tuple[dict[str, Any], dict[str, Any]]:
    left, right = request.param
    return (
        json.loads(left.read_text(encoding="utf-8")),
        json.loads(right.read_text(encoding="utf-8")),
    )


def test_the_fixtures_actually_pair_up() -> None:
    """A parity suite that silently compares nothing is worse than no parity suite."""
    assert len(PAIRS) >= 3


def test_same_duration_and_outcome(pair: tuple[dict[str, Any], dict[str, Any]]) -> None:
    od, st = pair
    assert od["duration"] == st["durationSeconds"]
    assert od["radiant_win"] is st["didRadiantWin"]


def test_same_picks(pair: tuple[dict[str, Any], dict[str, Any]]) -> None:
    od, st = pair
    left, right = opendota.snapshot_at(od, 0), stratz.snapshot_at(st, 0)
    assert sorted(left.radiant_picks) == sorted(right.radiant_picks)
    assert sorted(left.dire_picks) == sorted(right.dire_picks)


def test_same_building_sequence(pair: tuple[dict[str, Any], dict[str, Any]]) -> None:
    """The strong form of the npc id check, and the reason this file exists.

    Compared as an ordered sequence rather than per minute, which makes it immune to the
    one-second clock skew: every building must fall in the same order and be the same
    building on both sides.
    """
    od, st = pair
    from_names = [
        parse_building_key(str(event.get("key") or ""))
        for event in od["objectives"]
        if event.get("type") == "building_kill"
    ]
    from_ids = [parse_npc_id(event["npcId"]) for event in st["towerDeaths"]]

    assert [k for k in from_names if k] == [k for k in from_ids if k]


def test_building_counts_track_each_other(pair: tuple[dict[str, Any], dict[str, Any]]) -> None:
    od, st = pair
    for minute in range(od["duration"] // 60 + 1):
        left, right = opendota.snapshot_at(od, minute), stratz.snapshot_at(st, minute)
        for a, b in (
            (left.radiant.tower_count, right.radiant.tower_count),
            (left.dire.tower_count, right.dire.tower_count),
            (left.radiant.barracks_count, right.radiant.barracks_count),
            (left.dire.barracks_count, right.dire.barracks_count),
        ):
            assert abs(a - b) <= TOLERANCE, f"minute {minute}: {a} vs {b}"


def test_kills_track_each_other(pair: tuple[dict[str, Any], dict[str, Any]]) -> None:
    """Guards the other half of the alignment. If the kill events were read with the leads
    arrays' offset, every score would be a minute early and the gap would far exceed one.
    """
    od, st = pair
    for minute in range(od["duration"] // 60 + 1):
        left, right = opendota.snapshot_at(od, minute), stratz.snapshot_at(st, minute)
        assert abs(left.radiant.score - right.radiant.score) <= TOLERANCE, minute
        assert abs(left.dire.score - right.dire.score) <= TOLERANCE, minute


def test_the_winning_side_leads_in_both_at_the_end(
    pair: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    """The one thing the two gold series must agree on. Earlier in the match they need not:
    a side can be ahead on earned gold and behind on net worth after a round of buybacks.
    """
    od, st = pair
    last = od["duration"] // 60
    assert (opendota.snapshot_at(od, last).gold_adv > 0) == (
        stratz.snapshot_at(st, last).gold_adv > 0
    )


def test_the_two_sources_are_not_expected_to_match_on_gold(
    pair: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    """Written down so nobody 'fixes' the adapter to make the two agree.

    OpenDota's `radiant_gold_adv` is the sum of per-player earned gold; STRATZ's leads array
    is the sum of per-player net worth. Both identities are asserted here, which is what
    makes them different numbers on purpose rather than a bug in one of them.
    """
    od, st = pair
    earned = [
        sum(p["gold_t"][m] for p in od["players"] if p["player_slot"] < 128)
        - sum(p["gold_t"][m] for p in od["players"] if p["player_slot"] >= 128)
        for m in range(od["duration"] // 60)
    ]
    assert earned == od["radiant_gold_adv"][: len(earned)]

    net_worth = [
        sum(p["stats"]["networthPerMinute"][m] for p in st["players"] if p["isRadiant"])
        - sum(p["stats"]["networthPerMinute"][m] for p in st["players"] if not p["isRadiant"])
        for m in range(od["duration"] // 60)
    ]
    assert net_worth == st["radiantNetworthLeads"][1 : len(net_worth) + 1]
