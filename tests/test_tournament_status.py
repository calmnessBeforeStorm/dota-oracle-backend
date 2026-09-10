"""Which tournaments count as running (F3, spec section 8.1).

The calendar had no "current" tab in any meaningful sense, and not because of missing data:
status was derived from `max(start_time)` - the moment the last map *began*. That is in the
past by definition, so every league fell into "past", including one whose match was on
screen at that second. Measured on the live database: 10 games being polled, 0 tournaments
reported as current.

Two signals now answer it, and they fail in opposite directions, which is why both are
here. The live feed is exact but evaporates: it lives in Redis with a two-minute TTL, so a
poller that stopped takes every "current" tournament with it. A recent match is inexact but
durable: it survives a restart and covers the hours between two games of the same event.
"""

from datetime import UTC, datetime, timedelta

from app.api.tournament_status import RECENT_MATCH_WINDOW, status_of

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


class TestRunning:
    def test_a_league_with_a_game_on_air_is_current(self) -> None:
        # The exact signal: the poller is looking at it right now.
        assert (
            status_of(
                first=NOW - timedelta(days=2), last=NOW - timedelta(days=2), now=NOW, is_live=True
            )
            == "current"
        )

    def test_a_league_that_played_today_is_current(self) -> None:
        # The durable one: between two games of the same event nothing is on air.
        assert (
            status_of(
                first=NOW - timedelta(days=3), last=NOW - timedelta(hours=4), now=NOW, is_live=False
            )
            == "current"
        )

    def test_the_window_is_generous_enough_to_span_a_night(self) -> None:
        # A tournament that played its last game yesterday evening is still running today.
        last = NOW - RECENT_MATCH_WINDOW + timedelta(minutes=1)
        assert (
            status_of(first=NOW - timedelta(days=3), last=last, now=NOW, is_live=False) == "current"
        )


class TestPast:
    def test_a_league_whose_last_game_is_old_is_past(self) -> None:
        last = NOW - RECENT_MATCH_WINDOW - timedelta(minutes=1)
        assert status_of(first=NOW - timedelta(days=9), last=last, now=NOW, is_live=False) == "past"

    def test_a_finished_league_stays_past_even_on_a_stale_feed(self) -> None:
        assert (
            status_of(
                first=NOW - timedelta(days=90),
                last=NOW - timedelta(days=80),
                now=NOW,
                is_live=False,
            )
            == "past"
        )


class TestUpcoming:
    def test_a_league_whose_first_game_is_in_the_future_is_upcoming(self) -> None:
        # Nothing produces such a row today - every match we hold has been played - but the
        # branch is the one a schedule feed would light up, and it must not be reachable by
        # accident from a match that merely started recently.
        assert (
            status_of(
                first=NOW + timedelta(days=2), last=NOW + timedelta(days=5), now=NOW, is_live=False
            )
            == "upcoming"
        )

    def test_a_live_game_beats_a_future_first_match(self) -> None:
        # Contradictory inputs: something is on air, so it has started, whatever the dates
        # say. Believing the schedule here would hide a running tournament.
        assert (
            status_of(
                first=NOW + timedelta(days=2), last=NOW + timedelta(days=5), now=NOW, is_live=True
            )
            == "current"
        )


class TestMissingDates:
    def test_no_dates_and_no_feed_is_past(self) -> None:
        # A league we hold matches for but cannot date. Calling it current would put an
        # unknown at the top of the calendar; "past" is the honest default.
        assert status_of(first=None, last=None, now=NOW, is_live=False) == "past"

    def test_no_dates_but_a_live_game_is_current(self) -> None:
        assert status_of(first=None, last=None, now=NOW, is_live=True) == "current"
