"""Identifiers for what produced a raw payload - and the map of where our data comes from.

`raw_matches` is unique on (match_id, source), so these strings decide what overwrites what.
The list endpoint and the match-detail endpoint of the same provider are deliberately
separate sources: the summary from /proMatches must not be clobbered by the full match, and
losing either would mean re-spending quota to get it back.

The four platforms
==================

Each client module carries its own limits; this is the one place that shows the whole
picture, because the interesting part is not what each provides but what each *cannot*.

**Valve / Steam Web API** (`clients/steam.py`) - the live channel, and the only free one.
`GetLiveLeagueGames` is the scoreboard the poller reads every 20-30s: score, net worth,
buildings, draft-so-far, the broadcast delay. `GetRealtimeStats` needs a `server_steam_id`
and is therefore only reachable for a game already found. Cannot give history, cannot give
league *names* (an id and nothing else), and hands out negative team ids to line-ups
assembled for a single game.

**OpenDota** (`clients/opendota.py`) - the archive and the reference books. `/proMatches`
walks the history of played matches a hundred at a time; `/matches/{id}` gives one full
match; `/leagues`, `/proPlayers` and `/constants/heroes` are the reference books, one call
each for the lot. 50k calls a month, 60 a minute, and the daily allowance is what actually
stops a run. Its per-minute series is *earned gold*, which is not the quantity the live
scoreboard reports - which is why it is not what we train on.

**STRATZ** (`clients/stratz.py`) - per-minute series, and since 27.08.2026 the primary
source of them. Net worth rather than earned gold, so the offline and the live paths
describe the same thing. Also the draft (`pickBans`) and buildings (`towerDeaths`). On our
token every list under `playbackData` comes back empty and Roshan events are absent
entirely; the bulk `matches(ids:)` query needs an admin token, so maps are fetched one at a
time. The measured ceiling is ~550-650 maps an hour across all consumers, not the ~2000 the
rate limit suggests.

**Liquipedia** (`clients/liquipedia.py`) - the only authority on what the product is about:
Tier 1 marking, tournament stages, and the series FORMAT. Valve data cannot tell a Bo2 from
two Bo1s, so without this there is no Bo2 and no draw. It appears in no `RawSource` below
because it produces no match payloads - its output lands in `leagues`, `tournament_stages`
and `league_mappings`. Terms of use are enforced by IP ban: custom User-Agent, ~1 request /
2s, and CC-BY-SA attribution wherever the data is shown.

What no platform gives us
-------------------------

A schedule of matches that have not been played yet, and this is a wall rather than a gap.
Valve and OpenDota report only what has finished or is running. STRATZ is the same. Only
Liquipedia knows a schedule, and it keeps its tournament lists in LPDB, generated: they are
absent from page wikitext and exist solely inside rendered markup. An LPDB key was requested
on 2026-09-10 and refused - Liquipedia does not grant API access to projects that predict
match outcomes, which it files alongside betting regardless of monetisation.

Underneath the sources there is a schema wall too: `League` is keyed on Valve's `league_id`,
and a tournament that has not started has not been assigned one, so we learn a league exists
only once one of its matches has been played.

That is why the tournament calendar offers "current" and "past" only, why the tournament
page has no bracket, and why the calendar links to Liquipedia for a schedule instead of
reproducing one.
"""

from enum import StrEnum


class RawSource(StrEnum):
    OPENDOTA_PRO_MATCHES = "opendota_pro_matches"  # GET /proMatches, one row per summary
    OPENDOTA_MATCH = "opendota_match"  # GET /matches/{id}, the full payload
    STRATZ_MATCH = "stratz_match"
    STEAM_MATCH_DETAILS = "steam_match_details"
    #: GetLiveLeagueGames, the primary live channel (spec section 2.4/C1).
    STEAM_LIVE_LEAGUE_GAMES = "live_league_games"
    #: GetRealtimeStats, only reachable when a server_steam_id is known.
    STEAM_REALTIME_STATS = "realtime_stats"


class Checkpoint(StrEnum):
    """Keys in `ingest_checkpoints`. One row per resumable walk."""

    #: How deep into history the backwards walk has gone: the lowest match id stored.
    OPENDOTA_PRO_MATCHES = "opendota_pro_matches"
    #: The highest match id ever stored. The backwards walk never sees matches played after
    #: it started, so the catch-up run tracks the other end of the range separately - one
    #: cursor cannot describe a window that grows at both ends.
    OPENDOTA_PRO_MATCHES_NEWEST = "opendota_pro_matches_newest"
