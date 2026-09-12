"""Which slice of the world a prediction belongs to (design 2026-09-11-pro-segment).

Two sources, answering different questions. Valve's league tier (OpenDota `/leagues`)
separates professional from amateur and exists the moment a league does, but it cannot tell
Tier 1 from Tier 3: measured 11.09.2026, 30 of 32 Liquipedia Tier 1 leagues are plain
`professional`, like 11 Tier 3 ones, and `premium` is The International and almost nothing
else. Liquipedia separates Tier 1, but only for leagues somebody has mapped.

So Valve gates and Liquipedia ranks. The rule lives here once - in Python for the poller and
the feeds, and in SQL for the dashboard - and a test holds the two versions together.
"""

from typing import Literal

Segment = Literal["tier1", "pro", "excluded"]
SEGMENTS: tuple[Segment, ...] = ("tier1", "pro", "excluded")

PRO_VALVE_TIERS: tuple[str, ...] = ("professional", "premium")
EXCLUDED_VALVE_TIERS: tuple[str, ...] = ("excluded", "amateur")
VALVE_TIERS: tuple[str, ...] = PRO_VALVE_TIERS + EXCLUDED_VALVE_TIERS

LIQUIPEDIA_UNKNOWN = "unknown"


def display_tier(valve_tier: str | None, liquipedia_tier: str | None) -> str:
    """The tier a reader sees.

    Liquipedia's, except that an unmapped `premium` league reads as Tier 1: section 3 of the
    spec names this fallback, and without it the next International would sit under
    "unmarked" until somebody ran `map-leagues` by hand.
    """
    tier = liquipedia_tier or LIQUIPEDIA_UNKNOWN
    if tier == LIQUIPEDIA_UNKNOWN and valve_tier == "premium":
        return "tier1"
    return tier


def is_pro(valve_tier: str | None) -> bool:
    return valve_tier in PRO_VALVE_TIERS


def segment_of(valve_tier: str | None, liquipedia_tier: str | None) -> Segment | None:
    """Tier 1, Pro, Excluded - or None when Valve's tier is not known.

    None is not folded into any segment: a league missing from `/leagues` is a gap in our
    knowledge, and counting it as amateur or as pro would both be inventions.
    """
    if valve_tier in PRO_VALVE_TIERS:
        return "tier1" if display_tier(valve_tier, liquipedia_tier) == "tier1" else "pro"
    if valve_tier in EXCLUDED_VALVE_TIERS:
        return "excluded"
    return None
