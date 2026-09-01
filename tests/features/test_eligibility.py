"""Section 5.3 filtering: on metadata, never on the outcome.

The rule that matters most here is the one that is not implemented literally. The spec says
"leaver_status != 0"; the only non-zero value in the archive is 1, DISCONNECTED, which in
professional play means a player dropped and came back. Taken literally the filter deletes a
third of the training set for reconnects and catches no abandonment at all.
"""

from typing import Any

from app.features.eligibility import ABANDONMENT_FROM, ineligible_reason


def payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": 1,
        "durationSeconds": 2400,
        "radiantTeamId": 111,
        "direTeamId": 222,
        "players": [{"leaverStatus": "NONE"} for _ in range(10)],
    }
    base.update(overrides)
    return base


def test_an_ordinary_map_is_training_data() -> None:
    assert ineligible_reason(payload()) is None


def test_a_short_map_is_dropped() -> None:
    assert ineligible_reason(payload(durationSeconds=11 * 60)) == "shorter than 12 minutes"


def test_the_twelve_minute_floor_is_inclusive() -> None:
    assert ineligible_reason(payload(durationSeconds=12 * 60)) is None


def test_a_reconnect_is_not_an_abandonment() -> None:
    """2132 of 6129 maps carry this. Dropping them would be the single most expensive
    misreading of section 5.3 available, and it would look like following the spec."""
    players = [{"leaverStatus": "NONE"} for _ in range(9)] + [{"leaverStatus": "DISCONNECTED"}]

    assert ineligible_reason(payload(players=players)) is None


def test_an_actual_abandonment_is_dropped() -> None:
    for status in ("DISCONNECTED_TOO_LONG", "ABANDONED", "AFK", "NEVER_CONNECTED"):
        players = [{"leaverStatus": "NONE"} for _ in range(9)] + [{"leaverStatus": status}]
        assert ineligible_reason(payload(players=players)) == "somebody abandoned", status


def test_numeric_leaver_status_is_understood_too() -> None:
    """OpenDota sends the number, STRATZ the name, and the reference payloads are still here."""
    assert ineligible_reason(payload(players=[{"leaverStatus": 1} for _ in range(10)])) is None
    abandoned = [{"leaverStatus": ABANDONMENT_FROM} for _ in range(10)]
    assert ineligible_reason(payload(players=abandoned)) == "somebody abandoned"


def test_a_missing_leaver_status_is_not_an_abandonment() -> None:
    """Older payloads carry no such field, and absence is not evidence of leaving."""
    assert ineligible_reason(payload(players=[{} for _ in range(10)])) is None


def test_a_short_roster_is_dropped() -> None:
    assert ineligible_reason(payload(players=[{} for _ in range(9)])) == "incomplete roster"


def test_a_side_without_a_team_is_dropped() -> None:
    assert ineligible_reason(payload(direTeamId=None)) == "a side has no team"
    assert ineligible_reason(payload(radiantTeamId=0)) == "a side has no team"


def test_the_outcome_is_never_a_reason() -> None:
    """Section 5.3 in one test: a model that never sees a collapse overestimates the leader."""
    for won in (True, False):
        assert ineligible_reason(payload(didRadiantWin=won)) is None
