"""Building state (spec sections 6.1, 6.4, 12).

The leakage trap this module exists to avoid: `tower_status_radiant` on a finished match is
the state at the END of the game. Read it into a minute-15 snapshot and the model is told
who won. Every key below is a real one, taken from the objectives logs we hold.
"""

import pytest

from app.features.buildings import (
    BASE,
    FULL_BARRACKS,
    FULL_TOWERS,
    apply_kill,
    decode_bitmasks,
    full_base,
    parse_building_key,
    state_at,
)


class TestKeyParsing:
    @pytest.mark.parametrize(
        ("key", "is_radiant", "kind", "lane"),
        [
            ("npc_dota_goodguys_tower1_top", True, "tower", "top"),
            ("npc_dota_badguys_tower3_mid", False, "tower", "mid"),
            ("npc_dota_goodguys_melee_rax_bot", True, "barracks", "bot"),
            ("npc_dota_badguys_range_rax_top", False, "barracks", "top"),
        ],
    )
    def test_reads_side_kind_and_lane(
        self, key: str, is_radiant: bool, kind: str, lane: str
    ) -> None:
        """goodguys is Radiant, badguys is Dire - the only side marker in the log."""
        parsed = parse_building_key(key)
        assert parsed is not None
        assert (parsed.is_radiant, parsed.kind, parsed.lane) == (is_radiant, kind, lane)

    def test_ancient_towers_have_no_lane(self) -> None:
        """Both towers guarding the ancient are named tower4, so a side loses it twice."""
        parsed = parse_building_key("npc_dota_goodguys_tower4")
        assert parsed is not None
        assert (parsed.kind, parsed.lane) == ("tower", BASE)

    def test_fort_is_the_ancient(self) -> None:
        parsed = parse_building_key("npc_dota_badguys_fort")
        assert parsed is not None
        assert parsed.kind == "ancient"
        assert parsed.is_radiant is False

    @pytest.mark.parametrize("key", ["", "npc_dota_neutral_something", "garbage"])
    def test_unknown_keys_are_ignored_not_fatal(self, key: str) -> None:
        """Valve renames things. An unknown building leaves the state alone rather than
        corrupting it or stopping the whole featurisation."""
        assert parse_building_key(key) is None


class TestState:
    def test_starts_from_a_full_base(self) -> None:
        state = full_base()
        assert state.tower_count == 11  # 9 lane towers plus 2 at the ancient
        assert state.barracks_count == 6
        assert state.ancient_alive is True

    def test_applying_a_kill_never_goes_negative(self) -> None:
        state = full_base()
        kill = parse_building_key("npc_dota_goodguys_tower1_top")
        assert kill is not None
        for _ in range(10):
            state = apply_kill(state, kill)
        assert state.towers["top"] == 0


def kill_event(time: int, key: str) -> dict[str, object]:
    return {"type": "building_kill", "time": time, "key": key}


OBJECTIVES = [
    kill_event(600, "npc_dota_badguys_tower1_top"),
    kill_event(900, "npc_dota_badguys_tower1_mid"),
    kill_event(1800, "npc_dota_goodguys_tower1_bot"),
    kill_event(2400, "npc_dota_badguys_melee_rax_top"),
    kill_event(3000, "npc_dota_badguys_fort"),
    {"type": "CHAT_MESSAGE_ROSHAN_KILL", "time": 700, "team": 2},
]


class TestStateAt:
    def test_only_events_up_to_the_minute_count(self) -> None:
        """The assertion the whole module exists for."""
        at_ten = state_at(OBJECTIVES, minute=10, radiant=False)
        assert at_ten.tower_count == 10  # only the top tier 1 has fallen
        assert at_ten.ancient_alive is True

        at_fifty = state_at(OBJECTIVES, minute=50, radiant=False)
        assert at_fifty.tower_count == 9
        assert at_fifty.barracks_count == 5
        assert at_fifty.ancient_alive is False

    def test_sides_are_kept_apart(self) -> None:
        radiant = state_at(OBJECTIVES, minute=50, radiant=True)
        assert radiant.tower_count == 10  # radiant lost only its bottom tier 1
        assert radiant.ancient_alive is True

    def test_minute_zero_includes_pre_horn_events(self) -> None:
        state = state_at([kill_event(-30, "npc_dota_badguys_tower1_top")], 0, radiant=False)
        assert state.tower_count == 10

    def test_non_building_events_are_skipped(self) -> None:
        state = state_at([{"type": "CHAT_MESSAGE_AEGIS", "time": 10}], 10, radiant=True)
        assert state.tower_count == 11

    def test_empty_log_leaves_the_base_intact(self) -> None:
        assert state_at([], 30, radiant=True).tower_count == 11


class TestBitmasks:
    def test_untouched_base_decodes_to_everything(self) -> None:
        state = decode_bitmasks(0b11111111111, 0b111111)
        assert state.towers == FULL_TOWERS
        assert state.barracks == FULL_BARRACKS

    def test_empty_mask_is_a_razed_base(self) -> None:
        state = decode_bitmasks(0, 0)
        assert state.tower_count == 0
        assert state.barracks_count == 0

    def test_lane_bits_land_in_the_right_lane(self) -> None:
        """Bits 0-2 are top, 3-5 mid, 6-8 bot, 9-10 the ancient towers."""
        state = decode_bitmasks(0b00000000111, 0)
        assert state.towers["top"] == 3
        assert state.towers["mid"] == 0
        assert state.towers[BASE] == 0

    def test_real_live_value_decodes(self) -> None:
        """1926 and 51 were read off a live GetLiveLeagueGames scoreboard."""
        state = decode_bitmasks(1926, 51)
        assert 0 < state.tower_count < 11
        assert 0 < state.barracks_count <= 6
