"""A game that has not started is not a game state (spec sections 2.4, 12; invariant 13).

GetLiveLeagueGames lists a match from the moment the lobby forms. Until the horn the entry
carries no `scoreboard` at all, and every field then falls back to zero - including the
building bitmasks, where zero does not mean "unknown" but decodes to every tower and every
barracks destroyed.

Measured on our own prediction log: 84 of the 86 matches we hold both halves of had a logged
minute-0 prediction built from exactly that. Nearly every match on the site opened by telling
the model both bases had been razed, and nothing rejected it, because a fabricated state is
indistinguishable from a real one once it has been built.
"""

from typing import Any

import pytest

from app.features.adapters.steam import from_live_league_game, has_scoreboard
from app.features.live import build_live_features

STARTED: dict[str, Any] = {
    "match_id": 8_000_000_001,
    "scoreboard": {
        "duration": 1230.0,
        "radiant": {
            "score": 12,
            "tower_state": 2047,
            "barracks_state": 63,
            "players": [{"net_worth": 14000}, {"net_worth": 12000}],
        },
        "dire": {
            "score": 7,
            "tower_state": 1975,
            "barracks_state": 63,
            "players": [{"net_worth": 12000}, {"net_worth": 11000}],
        },
    },
}

#: What the API actually returns during the draft: an entry with teams and a league, and no
#: scoreboard whatsoever.
DRAFTING: dict[str, Any] = {
    "match_id": 8_000_000_002,
    "league_id": 17_000,
    "radiant_team": {"team_id": 1, "team_name": "Alpha"},
    "dire_team": {"team_id": 2, "team_name": "Beta"},
}


class TestDetection:
    def test_a_game_in_progress_is_recognised(self) -> None:
        assert has_scoreboard(STARTED)

    def test_a_drafting_game_is_not(self) -> None:
        assert not has_scoreboard(DRAFTING)

    def test_an_empty_scoreboard_is_not_a_scoreboard(self) -> None:
        """Present-but-empty has to count as absent too. Every field would default the same
        way, so distinguishing them would only preserve the bug under a different shape."""
        assert not has_scoreboard({"match_id": 1, "scoreboard": {}})

    def test_a_half_populated_scoreboard_is_not_enough(self) -> None:
        """Not a shape the API has ever produced - across 16303 stored payloads it emitted
        either a complete scoreboard or none - but it fabricates the same state through a
        different door, and the check is a dictionary lookup."""
        assert not has_scoreboard({"match_id": 1, "scoreboard": {"radiant": {"score": 0}}})


class TestRefusal:
    def test_building_a_state_for_a_drafting_game_raises(self) -> None:
        with pytest.raises(ValueError, match="no scoreboard"):
            from_live_league_game(DRAFTING)

    def test_a_started_game_still_builds(self) -> None:
        state = from_live_league_game(STARTED)

        assert state.minute == 20
        assert state.radiant.tower_count == 11

    def test_the_state_it_refused_to_build_was_the_dangerous_one(self) -> None:
        """The precise damage, stated as a test so the reasoning survives.

        A zero bitmask decodes to no towers and no barracks on either side - a position in
        which the game is already over, handed to the model as minute zero of a match that
        had not begun.
        """
        # Refused now, so the damage is shown on the state the old code would have built:
        # a bitmask of zero on both sides.
        forced = from_live_league_game(
            dict(
                DRAFTING,
                scoreboard={
                    "duration": 0,
                    "radiant": {"tower_state": 0, "barracks_state": 0},
                    "dire": {"tower_state": 0, "barracks_state": 0},
                },
            )
        )
        features = build_live_features(forced)

        assert features["radiant_towers"] == 0
        assert features["dire_towers"] == 0
        assert features["barracks_diff"] == 0
        # And the entry it came from is refused before any of that can happen.
        assert not has_scoreboard(DRAFTING)
