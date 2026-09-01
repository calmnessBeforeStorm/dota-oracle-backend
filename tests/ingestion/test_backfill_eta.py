"""The estimate the backfill prints has to be one somebody can plan on.

It used to divide the remaining maps by the client's throttle, which is the rate a *burst*
runs at. Every burst here ends at a quota - STRATZ refused a 1200-map run after 557, OpenDota
refuses after ~685 in a day - so the number it printed was six times too small, and it was
printed at exactly the moment a person is deciding whether the backfill needs help.
"""

from app.ingestion.cli import (
    DETAIL_FETCH_PER_MINUTE,
    DETAIL_MAPS_PER_HOUR,
    sustained_eta_hours,
)
from app.ingestion.workers.details import DETAILS_PER_HOUR


def test_estimate_uses_the_hourly_budget_not_the_throttle() -> None:
    assert DETAIL_MAPS_PER_HOUR["stratz"] == float(DETAILS_PER_HOUR)
    assert sustained_eta_hours(22255, "stratz") == 22255 / DETAILS_PER_HOUR


def test_the_old_estimate_was_optimistic_by_several_times() -> None:
    """Guards the direction, so a future edit cannot quietly put the burst rate back."""
    remaining = 22255
    honest = sustained_eta_hours(remaining, "stratz")
    throttle_based = remaining / DETAIL_FETCH_PER_MINUTE["stratz"] / 60

    assert honest > throttle_based * 4


def test_opendota_is_bound_by_its_daily_allowance() -> None:
    """~685 maps a day, so a day of uptime is a day of uptime however fast a burst goes."""
    assert sustained_eta_hours(685, "opendota") == 24.0


def test_every_fetchable_source_has_a_sustained_rate() -> None:
    from app.ingestion.workers.details import DETAIL_SOURCES

    assert set(DETAIL_MAPS_PER_HOUR) == set(DETAIL_SOURCES)
