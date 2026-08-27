"""Adapter: STRATZ match -> GameState at a given minute (spec sections 2.3, 6.4).

The train-time path, and since 27.08.2026 the only one that feeds `match_snapshots`. It
replaced the OpenDota path for a reason worth restating, because the two look
interchangeable and are not: OpenDota's per-minute series is *earned gold*, while the live
scoreboard the serve path reads is *net worth*. STRATZ reports net worth, so train and
serve finally describe the same quantity. Measurements are in
docs/superpowers/specs/2026-08-27-stratz-adapter-design.md.

Nothing here reads a field that describes the end of the match. `towerStatusRadiant` and
`barracksStatusRadiant` exist in the STRATZ schema and are exactly the trap the OpenDota
adapter documents - the final state, which in a minute-15 row leaks the result (spec
section 12). They are not even in the query. Buildings are replayed from `towerDeaths`.

Roshan is absent, not forgotten: on our token `playbackData.roshanEvents` comes back empty
and the chat log carries no Roshan entries either, measured over 60 matches. That is what
took the three Roshan features out of the vector (see app/features/live.py).
"""

from collections.abc import Mapping
from typing import Any

from app.features.buildings import BuildingState, state_at_npc
from app.features.game_state import GameState, SeriesContext, TeamState


def is_parsed(match: dict[str, Any]) -> bool:
    """Whether the per-minute series are there to unroll.

    `parsedDateTime` alone is not enough: it is set on matches whose series still come back
    empty, and an empty series would silently produce a match of flat zeroes.
    """
    return bool(match.get("parsedDateTime")) and bool(match.get("radiantNetworthLeads"))


def _lead_at(series: list[int] | None, minute: int) -> int:
    """Read one of the `...Leads` arrays.

    They carry an extra leading element for the time before the horn, so minute N sits at
    index N+1. Measured against OpenDota on real matches, not inferred from array length -
    the kill arrays are the same length and are *not* offset.
    """
    if not series:
        return 0
    return int(series[min(minute + 1, len(series) - 1)])


def _at(series: list[int] | None, minute: int) -> int:
    """`networthPerMinute` is indexed by minute directly - no leading element."""
    if not series:
        return 0
    return int(series[min(minute, len(series) - 1)])


def _kills_before(player: dict[str, Any], minute: int) -> int:
    """Kills this player had made by `minute`, from the per-player event list.

    Not from the match-level `radiantKills` / `direKills` arrays: measured over 20 team
    sides those totals were high by exactly one on five of them, while these events matched
    Valve's own `players[].kills` on all twenty. Same window as the OpenDota adapter's
    `kills_log`, so the two sources can be compared minute for minute.
    """
    events = (player.get("stats") or {}).get("killEvents") or []
    return len([e for e in events if int(e.get("time", 0)) <= minute * 60])


def _players(match: dict[str, Any], radiant: bool) -> list[dict[str, Any]]:
    return [p for p in match.get("players") or [] if bool(p.get("isRadiant")) is radiant]


def _team_state_at(
    match: dict[str, Any], minute: int, radiant: bool, buildings: BuildingState
) -> TeamState:
    players = _players(match, radiant)
    net_worths = tuple(
        _at((player.get("stats") or {}).get("networthPerMinute"), minute) for player in players
    )
    return TeamState(
        score=sum(_kills_before(player, minute) for player in players),
        net_worth=sum(net_worths),
        towers_alive=buildings.towers,
        barracks_alive=buildings.barracks,
        ancient_alive=buildings.ancient_alive,
        player_net_worths=net_worths,
    )


def _picks(match: dict[str, Any], radiant: bool) -> tuple[int, ...]:
    """Hero ids of one side. Drafted before the horn, so known at every minute."""
    return tuple(int(p["heroId"]) for p in _players(match, radiant) if p.get("heroId"))


def snapshot_at(
    match: dict[str, Any],
    minute: int,
    series: SeriesContext | None = None,
    prematch_prior: float | None = None,
    prematch: Mapping[str, float] | None = None,
) -> GameState:
    """One training snapshot. Only information available at `minute` may be read."""
    deaths = match.get("towerDeaths") or []

    return GameState(
        match_id=int(match["id"]),
        minute=minute,
        # Replayed from towerDeaths, never read off towerStatus*: that field is the state
        # at the end of the match and would leak the result (spec section 12).
        radiant=_team_state_at(match, minute, True, state_at_npc(deaths, minute, radiant=True)),
        dire=_team_state_at(match, minute, False, state_at_npc(deaths, minute, radiant=False)),
        gold_adv=_lead_at(match.get("radiantNetworthLeads"), minute),
        xp_adv=_lead_at(match.get("radiantExperienceLeads"), minute),
        radiant_picks=_picks(match, radiant=True),
        dire_picks=_picks(match, radiant=False),
        series=series or SeriesContext(),
        prematch=prematch or {},
        prematch_prior=prematch_prior,
    )


def iter_snapshots(
    match: dict[str, Any],
    series: SeriesContext | None = None,
    min_minute: int = 0,
    prematch: Mapping[str, float] | None = None,
    prematch_prior: float | None = None,
) -> list[GameState]:
    """Unroll a parsed match into one snapshot per minute.

    Snapshots of one match are heavily correlated: any split must be by `match_id`,
    never by row (spec section 5.1).
    """
    if not is_parsed(match):
        raise ValueError(f"match {match.get('id')} is not parsed, no per-minute series")
    last_minute = int(match.get("durationSeconds", 0)) // 60
    return [
        snapshot_at(match, m, series, prematch_prior, prematch)
        for m in range(min_minute, last_minute + 1)
    ]
