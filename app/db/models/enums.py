"""Shared enums for the normalized layer."""

from enum import StrEnum


class SeriesFormat(StrEnum):
    """Series format (spec section 5.5). Source of truth is Liquipedia stage,
    NOT Valve's `series_type` - Bo2 is not representable there at all."""

    BO1 = "bo1"
    BO2 = "bo2"
    BO3 = "bo3"
    BO5 = "bo5"

    @property
    def max_games(self) -> int:
        return {"bo1": 1, "bo2": 2, "bo3": 3, "bo5": 5}[self.value]

    @property
    def can_draw(self) -> bool:
        """Only Bo2 can end 1-1. A single Dota 2 map always has a winner."""
        return self is SeriesFormat.BO2


class StageType(StrEnum):
    GROUP = "group"
    PLAYOFF = "playoff"
    SWISS = "swiss"


class LeagueTier(StrEnum):
    TIER1 = "tier1"
    TIER2 = "tier2"
    TIER3 = "tier3"
    UNKNOWN = "unknown"
