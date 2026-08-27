"""The leakage test, for the STRATZ path (spec sections 5.1, 11, 12).

Same contract as tests/features/test_leakage.py, different payload shape. Features for
minute N must be identical whether they are computed from the whole match or from a match
whose recording was cut off just after minute N. Anything that reads the future changes
when the future is removed.

This one exists because the STRATZ payload has its own end-of-match summaries to trip over:
`towerStatusRadiant`, `barracksStatusDire`, `didRadiantWin` and the player-level `networth`
are all sitting right next to the per-minute series.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from app.features.adapters.stratz import snapshot_at
from app.features.live import build_live_features

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "stratz"
MATCHES = sorted(FIXTURES.glob("match_*.json"))


@pytest.fixture(params=MATCHES, ids=lambda p: p.stem)
def match(request: pytest.FixtureRequest) -> dict[str, Any]:
    return json.loads(request.param.read_text(encoding="utf-8"))


def truncate(match: dict[str, Any], minute: int) -> dict[str, Any]:
    """The same match as it would have looked with the recording stopped after `minute`.

    The leads arrays keep one extra element because they start one before the horn; that
    asymmetry is the point of the adapter's alignment and has to survive truncation too.
    """
    cutoff = (minute + 1) * 60
    players = []
    for player in match.get("players") or []:
        trimmed = dict(player)
        stats = dict(trimmed.get("stats") or {})
        if stats.get("networthPerMinute"):
            stats["networthPerMinute"] = stats["networthPerMinute"][: minute + 1]
        stats["killEvents"] = [
            e for e in (stats.get("killEvents") or []) if int(e["time"]) < cutoff
        ]
        trimmed["stats"] = stats
        # None of these are knowable while the match is still being played.
        trimmed["networth"] = None
        players.append(trimmed)

    return {
        "id": match["id"],
        "parsedDateTime": match["parsedDateTime"],
        "durationSeconds": cutoff,
        "players": players,
        "radiantNetworthLeads": match["radiantNetworthLeads"][: minute + 2],
        "radiantExperienceLeads": match["radiantExperienceLeads"][: minute + 2],
        "towerDeaths": [e for e in match["towerDeaths"] if int(e["time"]) < cutoff],
        "didRadiantWin": None,
    }


class TestTruncationInvariance:
    @pytest.mark.parametrize("minute", [0, 5, 10, 15])
    def test_features_do_not_change_when_the_future_is_removed(
        self, match: dict[str, Any], minute: int
    ) -> None:
        if match["durationSeconds"] // 60 <= minute:
            pytest.skip("match ended before this minute")

        whole = build_live_features(snapshot_at(match, minute))
        cut = build_live_features(snapshot_at(truncate(match, minute), minute))

        differing = {k: (whole[k], cut[k]) for k in whole if whole[k] != cut[k]}
        assert not differing, f"features that read the future: {differing}"

    def test_every_minute_of_a_match_is_truncation_invariant(self, match: dict[str, Any]) -> None:
        for minute in range(match["durationSeconds"] // 60):
            whole = build_live_features(snapshot_at(match, minute))
            cut = build_live_features(snapshot_at(truncate(match, minute), minute))
            assert whole == cut, f"minute {minute} reads the future"
