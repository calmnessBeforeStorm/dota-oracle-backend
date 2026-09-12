"""The segment rule (design 2026-09-11-pro-segment, section 1).

Valve gates, Liquipedia ranks. Measured 11.09.2026: 30 of 32 Liquipedia Tier 1 leagues are
plain `professional` at Valve, exactly like 11 Tier 3 ones, and `premium` is The
International and almost nothing else - so Valve's tier can only say pro or not.
"""

import pytest
from sqlalchemy import String, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.segments import display_tier, display_tier_expr, is_pro, segment_expr, segment_of


@pytest.mark.parametrize(
    ("valve", "liquipedia", "expected"),
    [
        ("professional", "tier1", "tier1"),
        ("premium", "tier1", "tier1"),
        # Section 3 fallback: an unmapped premium league is The International.
        ("premium", "unknown", "tier1"),
        ("premium", None, "tier1"),
        ("premium", "tier2", "pro"),
        ("professional", "unknown", "pro"),
        ("professional", None, "pro"),
        ("professional", "tier3", "pro"),
        # Valve gates first: markup cannot pull an amateur league into Tier 1.
        ("excluded", "tier1", "excluded"),
        ("amateur", None, "excluded"),
        # Unknown Valve tier is no segment at all, not a guess.
        (None, "tier1", None),
        (None, None, None),
        ("something-new", "tier1", None),
    ],
)
def test_segment_of(valve: str | None, liquipedia: str | None, expected: str | None) -> None:
    assert segment_of(valve, liquipedia) == expected


@pytest.mark.parametrize(
    ("valve", "liquipedia", "expected"),
    [
        ("professional", "tier1", "tier1"),
        ("premium", "tier3", "tier3"),
        ("premium", "unknown", "tier1"),
        ("premium", None, "tier1"),
        ("professional", None, "unknown"),
        (None, "unknown", "unknown"),
        ("excluded", "tier2", "tier2"),
    ],
)
def test_display_tier(valve: str | None, liquipedia: str | None, expected: str) -> None:
    # Same argument order as segment_of: Valve first. Both are `str | None`, so a swap would
    # type-check; one order everywhere is what keeps it from happening.
    assert display_tier(valve, liquipedia) == expected


@pytest.mark.parametrize(
    ("valve", "expected"),
    [
        ("premium", True),
        ("professional", True),
        ("excluded", False),
        ("amateur", False),
        (None, False),
    ],
)
def test_is_pro(valve: str | None, expected: bool) -> None:
    assert is_pro(valve) is expected


VALVE_VALUES = [None, "premium", "professional", "amateur", "excluded", "something-new"]
LIQUIPEDIA_VALUES = [None, "unknown", "tier1", "tier2", "tier3"]


async def test_the_sql_rule_agrees_with_the_python_rule(session: AsyncSession) -> None:
    """Two copies of one rule. The dashboard reads the SQL one and the feed the Python one;
    if they drift apart, a match is Tier 1 on one page and Pro on the next."""
    for valve in VALVE_VALUES:
        for liquipedia in LIQUIPEDIA_VALUES:
            valve_param = literal(valve, String())
            liquipedia_param = literal(liquipedia, String())
            row = (
                await session.execute(
                    select(
                        segment_expr(valve_param, liquipedia_param),
                        display_tier_expr(valve_param, liquipedia_param),
                    )
                )
            ).one()
            assert row[0] == segment_of(valve, liquipedia), (valve, liquipedia)
            assert row[1] == display_tier(valve, liquipedia), (valve, liquipedia)
