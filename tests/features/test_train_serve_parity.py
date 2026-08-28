"""Train/serve parity (spec sections 6.4, 12).

The single largest risk in this project after leakage. The training set is built from STRATZ
match payloads; the live service reads Steam's GetLiveLeagueGames. The two share no field
names, compute their numbers differently, and sample at different instants. If they drift
apart the model is quietly worse in production than in the notebook, and nothing in the
metrics says why.

This used to be a `skip` with a note saying the fixtures could not exist until the live
poller had run. They exist now: `resolve-outcomes` fetches the STRATZ payload for matches
the poller has already predicted, so for the first time we hold both halves of the same
match. `tests/fixtures/train_serve_pairs.json` is three real matches - each one's STRATZ
payload beside the features actually served for it, minute by minute.

What the numbers below mean, measured over 86 paired matches and 3067 minute-pairs before
the thresholds were chosen:

  - `gold_adv` agrees closely: median difference 213, p90 1136. This is the reassuring
    result, and it is the one that justifies the move to STRATZ - both sides now measure
    net worth. The OpenDota series they replaced measured accumulated gold earned, which
    is a different quantity that diverged by 2987 late in a match.
  - Buildings agree exactly in 81% of minutes and within one in 98%. The remainder is the
    two sources noticing a tower fall at slightly different moments.
  - Kills are the loosest, exact in 61%: a live scoreboard reports a kill when it happens,
    while `killEvents` timestamps are replay times, and the two cross a minute boundary in
    different places.

The thresholds are set with margin around those measurements. They are a drift alarm, not a
specification: the point is that a change which breaks the correspondence gets caught here
rather than in production three weeks later.
"""

import json
import statistics
from pathlib import Path
from typing import Any

import pytest

from app.features.adapters import steam
from app.features.adapters.stratz import snapshot_at
from app.features.live import FEATURE_ORDER, build_live_features

FIXTURE = Path(__file__).parent.parent / "fixtures" / "train_serve_pairs.json"

#: The features read out of a match payload, and so the only ones two sources can disagree
#: about. Everything else in the vector is looked up from our own tables on both sides.
COMPARED = (
    "minute",
    "log_minute",
    "gold_adv",
    "gold_adv_norm",
    "tower_diff",
    "barracks_diff",
    "radiant_towers",
    "dire_towers",
    "kill_diff",
    "net_worth_diff",
    "radiant_nw_spread",
    "dire_nw_spread",
)

REALTIME_SAMPLE: dict[str, object] = {
    "match": {"matchid": 7000000001, "server_steam_id": 90000000000000000, "game_time": 1230},
    "teams": [
        {
            "team_number": 2,
            "score": 12,
            "net_worth": 52000,
            "players": [{"net_worth": 14000}, {"net_worth": 12000}, {"net_worth": 10000}],
        },
        {
            "team_number": 3,
            "score": 7,
            "net_worth": 44000,
            "players": [{"net_worth": 12000}, {"net_worth": 11000}, {"net_worth": 9000}],
        },
    ],
    "buildings": [
        {"team": 2, "type": 0, "lane": 1, "tier": 1, "destroyed": False},
        {"team": 2, "type": 0, "lane": 2, "tier": 1, "destroyed": False},
        {"team": 3, "type": 0, "lane": 1, "tier": 1, "destroyed": True},
        {"team": 3, "type": 1, "lane": 2, "destroyed": False},
    ],
    "graph_data": {"graph_gold": [0, 500, 8000]},
}


def test_steam_adapter_produces_a_complete_feature_set() -> None:
    state = steam.from_realtime_stats(REALTIME_SAMPLE)
    assert state.minute == 20  # game_time 1230s
    assert state.gold_adv == 8000  # last point of graph_gold
    features = build_live_features(state)
    assert set(features) == set(FEATURE_ORDER)


def test_steam_adapter_counts_only_living_buildings() -> None:
    state = steam.from_realtime_stats(REALTIME_SAMPLE)
    assert state.radiant.tower_count == 2
    assert state.dire.tower_count == 0  # its only tower is destroyed
    assert state.dire.barracks_count == 1


@pytest.fixture(scope="module")
def paired() -> list[dict[str, Any]]:
    """Real matches for which we hold both the served features and the STRATZ payload."""
    return list(json.loads(FIXTURE.read_text(encoding="utf-8")))


def compare(paired: list[dict[str, Any]], feature: str) -> list[float]:
    """Absolute differences between the served value and the offline one, per minute.

    Minute 0 is excluded throughout: it is the pre-horn snapshot, and the live entry has no
    scoreboard there at all. That case has its own guard - see
    `tests/features/test_live_state_guard.py` - and is not a parity question.
    """
    out: list[float] = []
    for pair in paired:
        match = pair["match"]
        last_minute = int(match["durationSeconds"]) // 60
        for raw_minute, live in pair["live"].items():
            minute = int(raw_minute)
            if minute < 1 or minute > last_minute:
                continue
            offline = build_live_features(snapshot_at(match, minute))
            out.append(abs(float(live[feature]) - float(offline[feature])))
    return out


def percentile(values: list[float], p: float) -> float:
    return sorted(values)[int(p * len(values))]


class TestTheFixtureItself:
    def test_it_holds_several_real_matches(self, paired: list[dict[str, Any]]) -> None:
        """One match could agree by luck, and a fixture that shrank to nothing would make
        every assertion below pass vacuously."""
        assert len(paired) >= 3
        assert sum(len(pair["live"]) for pair in paired) > 150

    def test_the_offline_side_produces_exactly_the_current_vector(
        self, paired: list[dict[str, Any]]
    ) -> None:
        match = paired[0]["match"]

        assert set(build_live_features(snapshot_at(match, 20))) == set(FEATURE_ORDER)

    def test_every_compared_feature_is_still_in_the_vector(self) -> None:
        """Guards the list above from going stale in the quiet direction: a feature renamed
        out of `FEATURE_ORDER` would otherwise keep being "compared" against nothing."""
        assert set(COMPARED) <= set(FEATURE_ORDER)

    def test_every_compared_feature_was_served(self, paired: list[dict[str, Any]]) -> None:
        """The features parity is actually about, and only those.

        `COMPARED` is the part of the vector that is read out of a match payload, and it is
        the only part that can drift between two sources reading two different payloads.
        The rest - series context, venue, the pre-match block - is looked up from our own
        tables on both sides, so it is identical by construction and has nothing to do with
        skew.

        Whole-vector equality was the first version of this test and it was wrong twice
        over. These rows carry `roshan_kills`, `aegis_holder` and `roshan_respawn_in`, gone
        since the vector went from 30 features to 27; and they predate `is_lan_known` and
        `series_format_known`, added later still. The prediction log is not schema-stable -
        it spans every feature set ever served, and `model_version` did not always change
        when the vector did. Asserting on the whole vector would fail every time the vector
        legitimately changed, which is how a test gets weakened until it means nothing.

        A *compared* feature missing from a served row is the case that must never pass:
        that is the skew that costs the most, because it reads as a working model right up
        until deployment.
        """
        for pair in paired:
            for minute, served in pair["live"].items():
                missing = set(COMPARED) - set(served)
                assert not missing, f"match {pair['match']['id']} minute {minute}: {missing}"


class TestParity:
    def test_gold_advantage_tracks(self, paired: list[dict[str, Any]]) -> None:
        """The strongest feature in the vector, so the one skew would hurt most.

        Measured median 213 over 86 matches. A regression here almost certainly means the
        two sides have gone back to measuring different quantities - net worth against
        gold earned - which is the mistake that cost the project its first dataset.
        """
        diffs = compare(paired, "gold_adv")

        assert statistics.median(diffs) < 600
        assert percentile(diffs, 0.9) < 2500

    def test_building_counts_track(self, paired: list[dict[str, Any]]) -> None:
        """Exact agreement is not required and never happens: the sources notice a tower
        falling at slightly different moments. Being off by more than one is different -
        that is a decoding disagreement, not a timing one."""
        radiant = compare(paired, "radiant_towers")
        dire = compare(paired, "dire_towers")
        both = [r + d for r, d in zip(radiant, dire, strict=True)]

        assert sum(1 for d in both if d == 0) / len(both) > 0.7
        assert sum(1 for d in both if d <= 1) / len(both) > 0.9
        assert max(both) <= 3

    def test_barracks_counts_track(self, paired: list[dict[str, Any]]) -> None:
        diffs = compare(paired, "barracks_diff")

        assert sum(1 for d in diffs if d <= 1) / len(diffs) > 0.9

    def test_kill_difference_tracks(self, paired: list[dict[str, Any]]) -> None:
        """The loosest of them, and understood: the live scoreboard reports a kill as it
        happens while `killEvents` carries replay timestamps, so the two land on opposite
        sides of a minute boundary often."""
        diffs = compare(paired, "kill_diff")

        assert sum(1 for d in diffs if d <= 2) / len(diffs) > 0.9
        assert max(diffs) <= 6

    def test_net_worth_spread_tracks(self, paired: list[dict[str, Any]]) -> None:
        """Derived from five per-player numbers rather than one team total, so it catches a
        per-player mismatch that the team aggregate would average away."""
        diffs = compare(paired, "radiant_nw_spread")

        assert statistics.median(diffs) < 200
        assert percentile(diffs, 0.9) < 600

    def test_the_minute_itself_is_never_off(self, paired: list[dict[str, Any]]) -> None:
        """Everything else is compared at a shared minute, so a disagreement here would
        silently loosen every other assertion in this file."""
        assert compare(paired, "minute") == [0.0] * len(compare(paired, "minute"))
