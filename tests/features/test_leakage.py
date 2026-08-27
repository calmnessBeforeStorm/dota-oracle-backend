"""The leakage test phase 3 is required to pass (spec sections 5.1, 11, 12).

Leakage is the failure mode that looks like success: metrics come out fantastic and
production is a disaster, with nothing in between to warn you. The check has to be
mechanical rather than a matter of care, because the payload is full of end-of-match
summaries that are trivial to reach for by accident.

The strong form is truncation. Features for minute N must be identical whether they are
computed from the whole match or from a match whose log has been cut off just after minute
N. Anything that reads the future changes when the future is removed.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from app.features.adapters.opendota import snapshot_at
from app.features.live import build_live_features

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "opendota"
MATCHES = sorted(FIXTURES.glob("match_*.json"))


@pytest.fixture(params=MATCHES, ids=lambda p: p.stem)
def match(request: pytest.FixtureRequest) -> dict[str, Any]:
    return json.loads(request.param.read_text(encoding="utf-8"))


def truncate(match: dict[str, Any], minute: int) -> dict[str, Any]:
    """The same match as it would have looked with the recording stopped after `minute`.

    Everything a later minute could reveal is removed: the event log, the per-minute series,
    the final building masks and the result itself.
    """
    cutoff = (minute + 1) * 60
    players = []
    for player in match.get("players") or []:
        trimmed = dict(player)
        for key in ("gold_t", "xp_t", "lh_t", "dn_t"):
            if trimmed.get(key):
                trimmed[key] = trimmed[key][: minute + 1]
        if trimmed.get("kills_log"):
            trimmed["kills_log"] = [
                k for k in trimmed["kills_log"] if int(k.get("time", 0)) < cutoff
            ]
        players.append(trimmed)

    return {
        **match,
        "players": players,
        "objectives": [
            o for o in (match.get("objectives") or []) if int(o.get("time", 0)) < cutoff
        ],
        "radiant_gold_adv": (match.get("radiant_gold_adv") or [])[: minute + 1],
        "radiant_xp_adv": (match.get("radiant_xp_adv") or [])[: minute + 1],
        # None of these exist while a match is still being played.
        "radiant_win": None,
        "duration": cutoff,
        "tower_status_radiant": None,
        "tower_status_dire": None,
        "barracks_status_radiant": None,
        "barracks_status_dire": None,
    }


class TestTruncationInvariance:
    @pytest.mark.parametrize("minute", [0, 5, 10, 20])
    def test_features_do_not_change_when_the_future_is_removed(
        self, match: dict[str, Any], minute: int
    ) -> None:
        """The strong form of the check: if any feature read past `minute`, cutting the
        match short would change it."""
        if int(match["duration"]) // 60 <= minute:
            pytest.skip("match ended before this minute")

        full = build_live_features(snapshot_at(match, minute))
        cut = build_live_features(snapshot_at(truncate(match, minute), minute))

        differing = {k: (full[k], cut[k]) for k in full if full[k] != cut[k]}
        assert not differing, f"features that read the future: {differing}"

    def test_every_minute_of_a_match_is_truncation_invariant(self, match: dict[str, Any]) -> None:
        last_minute = int(match["duration"]) // 60
        for minute in range(0, last_minute):
            full = build_live_features(snapshot_at(match, minute))
            cut = build_live_features(snapshot_at(truncate(match, minute), minute))
            assert full == cut, f"minute {minute} differs once the future is removed"


class TestEarlyMinutesCarryLittleSignal:
    def test_minute_zero_is_a_blank_slate(self, match: dict[str, Any]) -> None:
        """Nothing has happened yet, so the state features must be neutral. Measured across
        the whole dataset the minute-zero gold leader wins 48.7% of the time - chance."""
        features = build_live_features(snapshot_at(match, 0))

        assert features["tower_diff"] == 0
        assert features["barracks_diff"] == 0
        assert features["radiant_towers"] == 11
        assert features["dire_towers"] == 11
        assert features["kill_diff"] == 0
        assert features["net_worth_diff"] == 0


class TestSourceParity:
    """A feature may only exist if both sources can supply it (spec section 6.4).

    This is what removed the XP advantage. OpenDota gives radiant_xp_adv exactly; the live
    scoreboard gives neither cumulative XP nor a way back to it - measured over 400 stored
    matches, reconstructing level from XP lands within one level 53% of the time and the
    resulting xp_adv sits 25.6% off at the median. Exact in training and a quarter wrong in
    production is the definition of train/serve skew.
    """

    def test_the_live_path_can_produce_every_feature(self) -> None:
        from app.features.adapters.steam import from_live_league_game
        from app.features.live import FEATURE_ORDER

        # Shaped after a real GetLiveLeagueGames entry.
        game = {
            "match_id": 1,
            "scoreboard": {
                "duration": 1230.5,
                "roshan_respawn_timer": 81,
                "radiant": {
                    "score": 12,
                    "tower_state": 1926,
                    "barracks_state": 51,
                    "players": [{"net_worth": 14000}],
                },
                "dire": {
                    "score": 7,
                    "tower_state": 2047,
                    "barracks_state": 63,
                    "players": [{"net_worth": 11000}],
                },
            },
        }
        live = build_live_features(from_live_league_game(game))
        assert set(live) == set(FEATURE_ORDER)

    def test_xp_advantage_is_deliberately_absent(self) -> None:
        """Guards against it being added back for looking obviously useful."""
        from app.features.live import FEATURE_ORDER

        assert "xp_adv" not in FEATURE_ORDER
        assert "xp_adv_norm" not in FEATURE_ORDER

    def test_both_adapters_agree_on_the_feature_set(self, match: dict[str, Any]) -> None:
        from app.features.adapters.steam import from_live_league_game

        offline = build_live_features(snapshot_at(match, 20))
        live = build_live_features(
            from_live_league_game(
                {
                    "match_id": match["match_id"],
                    "scoreboard": {
                        "duration": 1230.0,
                        "radiant": {"score": 0, "tower_state": 2047, "barracks_state": 63},
                        "dire": {"score": 0, "tower_state": 2047, "barracks_state": 63},
                    },
                }
            )
        )
        assert set(offline) == set(live)
