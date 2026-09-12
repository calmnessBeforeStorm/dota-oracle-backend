"""The public live feed (design 2026-09-11-pro-segment, section 2).

The product promises Tier 1 predictions. Measured on production 11.09.2026: every one of the
249 scored live predictions came from a league Valve tags `excluded`. The poller keeps
predicting those - they are the control group - but the feed does not show them.
"""

from typing import Any

from app.api.routes.matches import public_feed


def entry(match_id: int, valve_tier: str | None, tier: str = "unknown") -> dict[str, Any]:
    return {"match_id": match_id, "valve_tier": valve_tier, "tier": tier}


def test_hides_amateur_and_unknown_leagues() -> None:
    feed = [
        entry(1, "professional"),
        entry(2, "excluded"),
        entry(3, None),
        entry(4, "premium", "tier1"),
        entry(5, "amateur"),
    ]
    assert [e["match_id"] for e in public_feed(feed, None)] == [1, 4]


def test_keeps_the_order_the_poller_wrote() -> None:
    feed = [entry(9, "professional"), entry(3, "professional"), entry(7, "premium")]
    assert [e["match_id"] for e in public_feed(feed, None)] == [9, 3, 7]


def test_the_tier_filter_applies_on_top_of_the_gate() -> None:
    feed = [
        entry(1, "professional", "tier1"),
        entry(2, "excluded", "tier1"),
        entry(3, "professional", "tier3"),
    ]
    assert [e["match_id"] for e in public_feed(feed, "tier1")] == [1]


def test_an_entry_cached_before_the_field_existed_is_hidden() -> None:
    # The cache lives 120 seconds; for that long after a deploy it holds entries without the
    # key. Hiding them is the honest reading of "tier unknown".
    assert public_feed([{"match_id": 1, "tier": "tier1"}], None) == []
