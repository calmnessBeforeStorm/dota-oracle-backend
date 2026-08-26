"""Train/serve parity (spec sections 6.4, 12).

The single largest risk in this project after leakage: the offline pipeline reads OpenDota
parsed replays, the live service reads Steam GetRealtimeStats, and the two payloads share
almost no field names. If the adapters drift apart, the model is quietly worse in production
than in the notebook and nothing in the metrics says why.

The real test, due in phase 5: take a finished match for which we hold BOTH our recorded
live snapshots and the OpenDota parse, build features from each, and assert they agree
within tolerance minute by minute. Fixtures for that do not exist until the live poller has
run, so what is asserted here is the structural half of the contract.
"""

import pytest

from app.features.adapters import steam
from app.features.live import FEATURE_ORDER, build_live_features

REALTIME_SAMPLE: dict[str, object] = {
    "match": {"matchid": 7000000001, "server_steam_id": 90000000000000000, "game_time": 1230},
    "teams": [
        {
            "team_number": 2,
            "score": 12,
            "net_worth": 52000,
            "players": [{"net_worth": 14000}, {"net_worth": 12000}, {"net_worth": 10000}],
        },
        {
            "team_number": 3,
            "score": 7,
            "net_worth": 44000,
            "players": [{"net_worth": 12000}, {"net_worth": 11000}, {"net_worth": 9000}],
        },
    ],
    "buildings": [
        {"team": 2, "type": 0, "lane": 1, "tier": 1, "destroyed": False},
        {"team": 2, "type": 0, "lane": 2, "tier": 1, "destroyed": False},
        {"team": 3, "type": 0, "lane": 1, "tier": 1, "destroyed": True},
        {"team": 3, "type": 1, "lane": 2, "destroyed": False},
    ],
    "graph_data": {"graph_gold": [0, 500, 8000]},
}


def test_steam_adapter_produces_a_complete_feature_set() -> None:
    state = steam.from_realtime_stats(REALTIME_SAMPLE)
    assert state.minute == 20  # game_time 1230s
    assert state.gold_adv == 8000  # last point of graph_gold
    features = build_live_features(state)
    assert set(features) == set(FEATURE_ORDER)


def test_steam_adapter_counts_only_living_buildings() -> None:
    state = steam.from_realtime_stats(REALTIME_SAMPLE)
    assert state.radiant.tower_count == 2
    assert state.dire.tower_count == 0  # its only tower is destroyed
    assert state.dire.barracks_count == 1


@pytest.mark.skip(reason="needs paired live-snapshot + OpenDota fixtures, phase 5")
def test_live_and_offline_features_agree_for_the_same_match() -> None:
    """Regression guard from spec section 6.4. Do not delete - implement it."""
    raise NotImplementedError
