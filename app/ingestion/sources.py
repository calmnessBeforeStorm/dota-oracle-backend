"""Identifiers for what produced a raw payload.

`raw_matches` is unique on (match_id, source), so these strings decide what overwrites what.
The list endpoint and the match-detail endpoint of the same provider are deliberately
separate sources: the summary from /proMatches must not be clobbered by the full match, and
losing either would mean re-spending quota to get it back.
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

    OPENDOTA_PRO_MATCHES = "opendota_pro_matches"
