"""Which maps belong in the training set (spec section 5.3).

Section 5.3 lists seven filters. Only some of them can be applied to this data, and the
measurements below are why - taken 2026-09-01 over the 6129 maps then in `match_snapshots`.

**Applied here**, because the signal is real:

    duration < 12 minutes          14 maps      the spec's stated floor
    fewer than ten players          0 maps      a guard; free today, not free forever
    a team id missing               0 maps      same
    somebody abandoned              0 maps      see below

**Deliberately not applied**, because on this data they filter our own coverage rather than
match quality - and a filter on coverage is the `is_conditional_game` mistake again, where a
feature turned out to mean "decisive **and** we mapped the league":

    league absent from Liquipedia   4384 maps (71%)
    league with no prize pool       4611 maps (75%)
    league with no organizer        5873 maps (96%)

Those fields are populated *by* the Liquipedia mapping, so "no organizer" overwhelmingly
means "we have not mapped this league", not "this league had no organizer". Applying them
would cut the sample to a fifth while the binding constraint on the model is sample size.
Revisit when league mapping covers most of the archive rather than a third of it.

    three or more stand-ins         not computable: `match_players.is_standin` is written as
                                    a hard-coded False (it needs roster history from
                                    Liquipedia), so the count is zero for every map and a
                                    filter on it would be a filter on nothing.

    players with fewer than 20-30   60-70% of maps in every month, because `match_players`
    professional games              only covers the ~7k maps we hold details for. That is a
                                    statement about the backfill, not about the players.
                                    Revisit when the detail backfill has caught up.

**Abandonment is `leaverStatus >= 2`, not `!= 0`.** The spec says "leaver_status != 0", which
sounds equivalent and is not. The only non-zero value present anywhere in the dataset is `1`,
DISCONNECTED - a player who dropped and reconnected, which in professional play is routine and
is followed by the game continuing normally. Measured: 2132 maps of 6129 carry a `1`, and
**not one** carries a `2` or worse. Read literally the rule would delete a third of the
training set for reconnects while catching no abandonment at all, which is the opposite of
what section 5.3 is for.
"""

from typing import Any

#: STRATZ leaver statuses, on OpenDota's numbering (see `ingestion.normalize`). Everything at
#: or above this means the player did not come back.
ABANDONMENT_FROM = 2

_LEAVER_STATUS = {
    "NONE": 0,
    "DISCONNECTED": 1,
    "DISCONNECTED_TOO_LONG": 2,
    "ABANDONED": 3,
    "AFK": 4,
    "NEVER_CONNECTED": 5,
    "NEVER_CONNECTED_TOO_LONG": 6,
}

#: Spec section 5.3: forfeits and disconnect-ridden games are filtered by metadata, never by
#: outcome. Twelve minutes is the stated floor.
MIN_DURATION_SECONDS = 12 * 60

REQUIRED_PLAYERS = 10


def _leaver_value(raw: Any) -> int:
    """STRATZ sends a name, OpenDota a number, and older payloads send nothing."""
    if raw is None:
        return 0
    if isinstance(raw, int):
        return raw
    return _LEAVER_STATUS.get(str(raw), 0)


def ineligible_reason(payload: dict[str, Any]) -> str | None:
    """Why this map is not training data, or None if it is.

    Reads the match payload rather than the normalized tables on purpose. `match_players` is
    written by `normalize`, which runs on its own schedule, so a map whose payload has arrived
    but whose rows have not would look like a map with no players - and would be dropped for
    a reason that is about our pipeline's timing rather than about the game.
    """
    if int(payload.get("durationSeconds") or 0) < MIN_DURATION_SECONDS:
        return "shorter than 12 minutes"

    players = payload.get("players") or []
    if len(players) < REQUIRED_PLAYERS:
        return "incomplete roster"

    if any(_leaver_value(p.get("leaverStatus")) >= ABANDONMENT_FROM for p in players):
        return "somebody abandoned"

    if not payload.get("radiantTeamId") or not payload.get("direTeamId"):
        return "a side has no team"

    return None
