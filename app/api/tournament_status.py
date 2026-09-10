"""Whether a tournament is running, finished, or still ahead (F3, spec section 8.1).

Kept out of the route because the previous version was wrong in a way that read as correct.
Status came from `max(start_time)` - the moment the last map *began* - compared against now.
That comparison is false for every league that has ever played, so "current" was
unreachable: on the live database the poller was watching 10 games while the calendar
reported 0 current tournaments.

Two signals answer it, and they are here together because they fail in opposite directions:

- **A game on air** is exact, and evaporates. The live feed lives in Redis under a
  two-minute TTL, so a poller that stopped takes every "current" tournament with it.
- **A recent match** is inexact, and durable. It survives a restart and covers the hours
  between two games of the same event, which is most of a tournament day.
"""

from datetime import datetime, timedelta

#: How long after its last map a tournament still counts as running.
#:
#: Wide enough to span a night between playing days - a tournament that finished at 22:00
#: is still running at noon the next day - and narrow enough that a finished event drops off
#: the tab within a day of its grand final. Tournaments play in blocks with gaps of hours,
#: so anything much tighter flickers between "current" and "past" all evening.
RECENT_MATCH_WINDOW = timedelta(hours=30)


def status_of(
    *,
    last: datetime | None,
    now: datetime,
    is_live: bool,
) -> str:
    """Either "current" or "past". There is no third answer.

    `is_live` wins over everything: if a game of this league is on air, the tournament has
    started, whatever its dates claim. Dates come from matches we hold, and a match can be
    missing; a game being polled cannot be argued with.

    With no dates at all the answer is "past" rather than "current". Such a league exists
    only because we hold matches without timestamps, and putting an unknown at the top of
    the calendar is the kind of flattery the accuracy dashboard is written to avoid.

    An "upcoming" branch used to sit here, waiting for a schedule feed. It could never fire:
    `first` is the earliest match we hold and we hold a match only once it has been played.
    The feed it was written for does not exist either - Liquipedia keeps tournament lists in
    LPDB, and access was requested on 2026-09-10 and refused - and underneath that, `League`
    is keyed on Valve's `league_id`, which an unstarted tournament has not been assigned.
    A branch that cannot be reached is not a placeholder, it is a claim that the product
    answers a question it cannot answer, so it is gone rather than pending.
    """
    if is_live:
        return "current"
    # The lower bound is not decoration. A match dated in the future makes `now - last`
    # negative, which passes any upper bound on its own and would report a tournament as
    # running on the strength of a bad timestamp.
    if last and timedelta(0) <= now - last <= RECENT_MATCH_WINDOW:
        return "current"
    return "past"
