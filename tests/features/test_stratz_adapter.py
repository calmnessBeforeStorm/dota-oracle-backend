"""The STRATZ adapter (spec sections 2.3, 6.4).

The alignment tests carry the weight here. The two families of per-minute arrays are offset
differently - measured, not derived - and getting one of them wrong shifts the strongest
feature by a minute without failing anything else.
"""

import json
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest

from app.features.adapters.stratz import is_parsed, iter_snapshots, snapshot_at

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "stratz"
MATCHES = sorted(FIXTURES.glob("match_*.json"))


@pytest.fixture(params=MATCHES, ids=lambda p: p.stem)
def match(request: pytest.FixtureRequest) -> dict[str, Any]:
    return json.loads(request.param.read_text(encoding="utf-8"))


class TestAlignment:
    """`radiantNetworthLeads` and `radiantExperienceLeads` carry an extra leading element
    for the time before the horn, so minute N sits at index N+1. The kill arrays do not.
    """

    def test_gold_adv_reads_the_leads_array_one_ahead(self, match: dict[str, Any]) -> None:
        leads = match["radiantNetworthLeads"]
        for minute in (0, 5, 10):
            assert snapshot_at(match, minute).gold_adv == leads[minute + 1]

    def test_xp_adv_reads_the_leads_array_one_ahead(self, match: dict[str, Any]) -> None:
        leads = match["radiantExperienceLeads"]
        for minute in (0, 5, 10):
            assert snapshot_at(match, minute).xp_adv == leads[minute + 1]

    def test_net_worth_reads_the_player_array_in_place(self, match: dict[str, Any]) -> None:
        expected = sum(
            p["stats"]["networthPerMinute"][7] for p in match["players"] if p["isRadiant"]
        )
        assert snapshot_at(match, 7).radiant.net_worth == expected

    def test_score_counts_per_player_kill_events(self, match: dict[str, Any]) -> None:
        """Not the match-level radiantKills array - it over-counts, see the query comment
        in app/ingestion/clients/stratz.py."""
        for minute in (0, 5, 12):
            state = snapshot_at(match, minute)
            for radiant, score in ((True, state.radiant.score), (False, state.dire.score)):
                expected = len(
                    [
                        e
                        for p in match["players"]
                        if p["isRadiant"] is radiant
                        for e in (p["stats"]["killEvents"] or [])
                        if e["time"] <= minute * 60
                    ]
                )
                assert score == expected

    def test_every_kill_event_is_accounted_for(self, match: dict[str, Any]) -> None:
        """The event list must reconcile with Valve's own per-player totals - that is what
        made it the better of the two kill sources STRATZ offers.

        Note this is the whole list, not the last snapshot: the score window closes at
        `minute * 60`, so kills in the final part-minute belong to no snapshot at all.
        """
        for player in match["players"]:
            assert len(player["stats"]["killEvents"] or []) == player["kills"]

    def test_gold_adv_equals_the_net_worth_difference(self, match: dict[str, Any]) -> None:
        """Both come from STRATZ and both are net worth, so unlike the OpenDota path they
        must agree exactly. A mismatch means an offset slipped in."""
        for minute in (0, 5, 10):
            state = snapshot_at(match, minute)
            assert state.gold_adv == state.radiant.net_worth - state.dire.net_worth


class TestSnapshots:
    def test_parsed_matches_are_recognised(self, match: dict[str, Any]) -> None:
        assert is_parsed(match)

    def test_an_unparsed_match_has_no_series(self) -> None:
        assert not is_parsed({"id": 1, "parsedDateTime": None})
        assert not is_parsed({"id": 1, "parsedDateTime": 123, "radiantNetworthLeads": []})

    def test_one_snapshot_per_minute(self, match: dict[str, Any]) -> None:
        snapshots = iter_snapshots(match)
        assert [s.minute for s in snapshots] == list(range(match["durationSeconds"] // 60 + 1))

    def test_picks_are_five_a_side(self, match: dict[str, Any]) -> None:
        state = snapshot_at(match, 0)
        assert len(state.radiant_picks) == 5
        assert len(state.dire_picks) == 5

    def test_buildings_start_whole_and_never_regrow(self, match: dict[str, Any]) -> None:
        snapshots = iter_snapshots(match)
        assert snapshots[0].radiant.tower_count == 11
        assert snapshots[0].dire.barracks_count == 6
        for earlier, later in pairwise(snapshots):
            assert later.radiant.tower_count <= earlier.radiant.tower_count
            assert later.dire.barracks_count <= earlier.dire.barracks_count

    def test_score_never_goes_backwards(self, match: dict[str, Any]) -> None:
        previous = 0
        for snapshot in iter_snapshots(match):
            assert snapshot.radiant.score >= previous
            previous = snapshot.radiant.score

    def test_roshan_stays_at_its_defaults(self, match: dict[str, Any]) -> None:
        """STRATZ carries no Roshan events, and the adapter must not invent any. These
        fields are out of the feature vector, so defaults here are honest rather than a
        silent zero standing in for real data."""
        state = snapshot_at(match, 10)
        assert state.roshan_kills == 0
        assert state.aegis_holder_is_radiant is None
        assert state.roshan_respawn_in is None

    def test_unparsed_match_is_refused(self) -> None:
        with pytest.raises(ValueError, match="not parsed"):
            iter_snapshots({"id": 7, "parsedDateTime": None})
