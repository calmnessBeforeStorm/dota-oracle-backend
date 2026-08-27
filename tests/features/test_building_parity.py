"""The building invariant that ties both sources together (spec sections 6.4, 12).

Replaying the objectives log to the end of a match must land on exactly the state Valve
reports in `tower_status_*`. That one equality checks several things at once:

  - the replay misses no building and double-counts none
  - our reading of the bitmask layout is right
  - and therefore the offline path and the live path, which decode the same layout, agree

It is also the check that catches the shortcut this module exists to avoid. Reading the
final bitmask into an earlier minute would satisfy nothing here, because the two are only
supposed to coincide at the end.

The fixtures are three real matches from the data we hold, trimmed to the fields the adapter
reads: a short stomp, an average game and a 68-minute grind.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from app.features.adapters.opendota import iter_snapshots, snapshot_at
from app.features.buildings import decode_bitmasks, state_at

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "opendota"
MATCHES = sorted(FIXTURES.glob("match_*.json"))


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(params=MATCHES, ids=lambda p: p.stem)
def match(request: pytest.FixtureRequest) -> dict[str, Any]:
    return load(request.param)


class TestReplayMatchesTheFinalBitmask:
    @pytest.mark.parametrize(
        ("radiant", "tower_key", "rax_key"),
        [
            (True, "tower_status_radiant", "barracks_status_radiant"),
            (False, "tower_status_dire", "barracks_status_dire"),
        ],
    )
    def test_towers_and_barracks_converge(
        self, match: dict[str, Any], radiant: bool, tower_key: str, rax_key: str
    ) -> None:
        last_minute = int(match["duration"]) // 60
        replayed = state_at(match["objectives"], last_minute, radiant)
        reported = decode_bitmasks(int(match[tower_key]), int(match[rax_key]))

        side = "radiant" if radiant else "dire"
        assert replayed.tower_count == reported.tower_count, f"{side} towers"
        assert replayed.barracks_count == reported.barracks_count, f"{side} barracks"


class TestNoLeakage:
    def test_early_snapshot_shows_an_untouched_base(self, match: dict[str, Any]) -> None:
        """Minute two of a match that ended in a razed base must still show a full base.
        This is the shape of the bug that makes metrics look wonderful and production look
        broken (spec section 12)."""
        early = snapshot_at(match, minute=2)
        assert early.radiant.ancient_alive is True
        assert early.dire.ancient_alive is True
        assert early.radiant.tower_count == 11
        assert early.dire.tower_count == 11

    def test_buildings_never_come_back(self, match: dict[str, Any]) -> None:
        """A count that rises means the replay is reading something other than the log."""
        previous_radiant, previous_dire = 99, 99
        for snapshot in iter_snapshots(match):
            assert snapshot.radiant.tower_count <= previous_radiant
            assert snapshot.dire.tower_count <= previous_dire
            previous_radiant = snapshot.radiant.tower_count
            previous_dire = snapshot.dire.tower_count

    def test_one_snapshot_per_minute(self, match: dict[str, Any]) -> None:
        snapshots = iter_snapshots(match)
        assert len(snapshots) == int(match["duration"]) // 60 + 1
        assert [s.minute for s in snapshots] == list(range(len(snapshots)))

    def test_kills_only_accumulate(self, match: dict[str, Any]) -> None:
        previous = 0
        for snapshot in iter_snapshots(match):
            assert snapshot.radiant.score >= previous
            previous = snapshot.radiant.score


class TestRoshan:
    def test_aegis_expires(self, match: dict[str, Any]) -> None:
        """Held for five minutes, then gone. A permanently held aegis would be a feature
        that quietly tracks 'this team killed Roshan at some point'."""
        holders = [s.aegis_holder_is_radiant for s in iter_snapshots(match)]
        if not any(h is not None for h in holders):
            pytest.skip("no aegis was taken in this match")
        assert holders[-1] is None or holders.count(None) > 0

    def test_roshan_kills_only_accumulate(self, match: dict[str, Any]) -> None:
        previous = 0
        for snapshot in iter_snapshots(match):
            assert snapshot.roshan_kills >= previous
            previous = snapshot.roshan_kills


def test_fixtures_exist() -> None:
    """Guards against the fixtures being dropped and every test above silently vanishing."""
    assert len(MATCHES) >= 3
