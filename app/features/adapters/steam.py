"""Adapter: Steam live data -> GameState (spec sections 2.4, 6.4).

This is the serve-time path. Its output must match the OpenDota adapter for the same
finished match within tolerance - that regression test is what keeps the model honest.

Two channels, and the primary one is not the one the spec assumed. `server_steam_id` turned
out to be absent from every live tournament game measured, so `GetRealtimeStats` is
unreachable for them and `GetLiveLeagueGames` carries the load. Its scoreboard is
self-sufficient, and its `tower_state` / `barracks_state` use the same bitmask layout as
OpenDota's final state - so buildings decode through the same code on both sides.
"""

from typing import Any

from app.features.buildings import LANES, BuildingState, decode_bitmasks
from app.features.game_state import GameState, SeriesContext, TeamState

RADIANT_TEAM_NUMBER = 2  # Valve numbers radiant 2, dire 3 in GetRealtimeStats
DIRE_TEAM_NUMBER = 3


def _buildings_from_list(buildings: list[dict[str, Any]], team_number: int) -> BuildingState:
    """GetRealtimeStats lists buildings individually rather than as a mask."""
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
    return BuildingState(towers=towers, barracks=barracks, ancient_alive=ancient_alive)


def _team_state(
    team: dict[str, Any], buildings: BuildingState, net_worth_key: str = "net_worth"
) -> TeamState:
    players = team.get("players", []) or []
    net_worths = tuple(int(p.get(net_worth_key, 0) or 0) for p in players)
    return TeamState(
        score=int(team.get("score", 0) or 0),
        net_worth=int(team.get("net_worth", 0) or 0) or sum(net_worths),
        towers_alive=buildings.towers,
        barracks_alive=buildings.barracks,
        ancient_alive=buildings.ancient_alive,
        player_net_worths=net_worths,
    )


def from_realtime_stats(
    payload: dict[str, Any],
    series: SeriesContext | None = None,
    prematch_prior: float | None = None,
) -> GameState:
    """Build a GameState from one GetRealtimeStats response.

    Only reachable when a `server_steam_id` is known, which for tournament games it usually
    is not - see the note in spec section 2.4/C1.
    """
    match = payload.get("match", {})
    teams = {t.get("team_number"): t for t in payload.get("teams", []) or []}
    buildings = payload.get("buildings", []) or []

    radiant = _team_state(
        teams.get(RADIANT_TEAM_NUMBER, {}), _buildings_from_list(buildings, RADIANT_TEAM_NUMBER)
    )
    dire = _team_state(
        teams.get(DIRE_TEAM_NUMBER, {}), _buildings_from_list(buildings, DIRE_TEAM_NUMBER)
    )

    graph_gold = (payload.get("graph_data") or {}).get("graph_gold") or []
    gold_adv = int(graph_gold[-1]) if graph_gold else radiant.net_worth - dire.net_worth

    return GameState(
        match_id=int(match.get("matchid", 0) or 0),
        minute=int(match.get("game_time", 0) or 0) // 60,
        radiant=radiant,
        dire=dire,
        gold_adv=gold_adv,
        # TODO(phase-5): GetRealtimeStats carries no XP series; derive from player levels.
        xp_adv=0,
        series=series or SeriesContext(),
        prematch_prior=prematch_prior,
    )


def _side(scoreboard: dict[str, Any], key: str) -> dict[str, Any]:
    return dict(scoreboard.get(key) or {})


def from_live_league_game(
    game: dict[str, Any],
    series: SeriesContext | None = None,
    prematch_prior: float | None = None,
) -> GameState:
    """Build a GameState from one GetLiveLeagueGames entry - the primary live channel.

    Its scoreboard carries per-player net worth, gold, level and XP per minute, per-team
    score and building bitmasks, and the Roshan respawn timer. `gold_adv` is summed from
    player net worth: unlike GetRealtimeStats there is no gold graph, and that difference
    is what the train/serve regression test has to keep honest.
    """
    scoreboard = game.get("scoreboard") or {}
    radiant_side = _side(scoreboard, "radiant")
    dire_side = _side(scoreboard, "dire")

    radiant = _team_state(
        radiant_side,
        decode_bitmasks(
            int(radiant_side.get("tower_state", 0) or 0),
            int(radiant_side.get("barracks_state", 0) or 0),
        ),
    )
    dire = _team_state(
        dire_side,
        decode_bitmasks(
            int(dire_side.get("tower_state", 0) or 0),
            int(dire_side.get("barracks_state", 0) or 0),
        ),
    )

    respawn = scoreboard.get("roshan_respawn_timer")
    return GameState(
        match_id=int(game.get("match_id", 0) or 0),
        minute=int(float(scoreboard.get("duration", 0) or 0)) // 60,
        radiant=radiant,
        dire=dire,
        gold_adv=radiant.net_worth - dire.net_worth,
        # No XP series in this payload either; levels are present and phase 5 will use them.
        xp_adv=0,
        roshan_respawn_in=int(respawn) if respawn else None,
        radiant_picks=tuple(int(h) for h in radiant_side.get("picks_hero_ids", ()) or ()),
        dire_picks=tuple(int(h) for h in dire_side.get("picks_hero_ids", ()) or ()),
        series=series or SeriesContext(),
        prematch_prior=prematch_prior,
    )
