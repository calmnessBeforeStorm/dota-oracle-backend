"""Adapter: Steam GetRealtimeStats payload -> GameState (spec sections 2.4/C2, 6.4).

This is the serve-time path. Its output must match the OpenDota adapter for the same
finished match within tolerance - that regression test is what keeps the model honest.
"""

from typing import Any

from app.features.game_state import GameState, SeriesContext, TeamState

LANES = ("top", "mid", "bot")
RADIANT_TEAM_NUMBER = 2  # Valve numbers radiant 2, dire 3 in GetRealtimeStats
DIRE_TEAM_NUMBER = 3


def _buildings_for(
    buildings: list[dict[str, Any]], team_number: int
) -> tuple[dict[str, int], dict[str, int], bool]:
    towers = dict.fromkeys(LANES, 0)
    barracks = dict.fromkeys(LANES, 0)
    ancient_alive = True
    for building in buildings:
        if building.get("team") != team_number or building.get("destroyed"):
            continue
        lane = {1: "top", 2: "mid", 3: "bot"}.get(building.get("lane", 0))
        kind = building.get("type")
        if kind == 0 and lane:  # tower
            towers[lane] += 1
        elif kind == 1 and lane:  # barracks
            barracks[lane] += 1
        elif kind == 2:  # ancient
            ancient_alive = True
    return towers, barracks, ancient_alive


def _team_state(
    team: dict[str, Any], buildings: list[dict[str, Any]], team_number: int
) -> TeamState:
    towers, barracks, ancient_alive = _buildings_for(buildings, team_number)
    players = team.get("players", []) or []
    return TeamState(
        score=int(team.get("score", 0)),
        net_worth=int(team.get("net_worth", 0)),
        towers_alive=towers,
        barracks_alive=barracks,
        ancient_alive=ancient_alive,
        player_net_worths=tuple(int(p.get("net_worth", 0)) for p in players),
    )


def from_realtime_stats(
    payload: dict[str, Any],
    series: SeriesContext | None = None,
    prematch_prior: float | None = None,
) -> GameState:
    """Build a GameState from one GetRealtimeStats response.

    `series` comes from GetLiveLeagueGames plus the resolved series format - Valve cannot
    tell us whether this is a Bo2 (spec section 5.5), so the caller resolves it.
    """
    match = payload.get("match", {})
    teams = {t.get("team_number"): t for t in payload.get("teams", []) or []}
    buildings = payload.get("buildings", []) or []

    radiant = _team_state(teams.get(RADIANT_TEAM_NUMBER, {}), buildings, RADIANT_TEAM_NUMBER)
    dire = _team_state(teams.get(DIRE_TEAM_NUMBER, {}), buildings, DIRE_TEAM_NUMBER)

    graph_gold = (payload.get("graph_data") or {}).get("graph_gold") or []
    gold_adv = int(graph_gold[-1]) if graph_gold else radiant.net_worth - dire.net_worth

    return GameState(
        match_id=int(match.get("matchid", 0)),
        minute=int(match.get("game_time", 0)) // 60,
        radiant=radiant,
        dire=dire,
        gold_adv=gold_adv,
        # TODO(phase-5): GetRealtimeStats carries no XP series; derive from player levels.
        xp_adv=0,
        series=series or SeriesContext(),
        prematch_prior=prematch_prior,
    )
