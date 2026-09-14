"""The live loop (spec sections 2.4, 5.5, 7.4, phase 5).

The payloads here are shaped after real GetLiveLeagueGames responses, including the parts
that surprised us: no `server_steam_id`, building state as bitmasks, and duration as a
float.
"""

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.enums import SeriesFormat
from app.db.models.reference import League
from app.ingestion.workers.live_poller import (
    UNKNOWN_LEAGUE,
    LeagueContext,
    _feed_entry,
    _league_context,
    _series_context,
    leagues_without_valve_tier,
    prediction_row,
)


def live_game(
    *,
    radiant_wins: int = 0,
    dire_wins: int = 0,
    duration: float = 1230.5,
    stream_delay: int = 300,
) -> dict[str, Any]:
    return {
        "match_id": 8968034437,
        "league_id": 19696,
        "lobby_id": 29992559167202101,
        "stream_delay_s": stream_delay,
        "radiant_series_wins": radiant_wins,
        "dire_series_wins": dire_wins,
        "series_type": 0,
        "radiant_team": {"team_id": 1, "team_name": "Radiant Side"},
        "dire_team": {"team_id": 2, "team_name": "Dire Side"},
        "scoreboard": {
            "duration": duration,
            "roshan_respawn_timer": 81,
            "radiant": {
                "score": 12,
                "tower_state": 1926,
                "barracks_state": 51,
                "players": [{"net_worth": 14000}, {"net_worth": 12000}],
            },
            "dire": {
                "score": 7,
                "tower_state": 2047,
                "barracks_state": 63,
                "players": [{"net_worth": 11000}, {"net_worth": 9000}],
            },
        },
    }


class TestSeriesContext:
    def test_known_format_marks_a_decisive_map(self) -> None:
        context = _series_context(live_game(radiant_wins=1, dire_wins=1), SeriesFormat.BO3)
        assert context.game_in_series == 3
        assert context.is_conditional_game is True

    def test_bo2_second_map_is_never_decisive(self) -> None:
        context = _series_context(live_game(radiant_wins=1, dire_wins=0), SeriesFormat.BO2)
        assert context.game_in_series == 2
        assert context.is_conditional_game is False

    def test_unknown_format_never_claims_a_decisive_map(self) -> None:
        """The bug this guards. An unknown format has to become something for the feature
        vector, and Bo1 is the least-claiming choice - but reading it back as knowledge
        turns a 1-0 series into a "decisive" second map that nobody knows to be one."""
        context = _series_context(live_game(radiant_wins=1, dire_wins=0), None)
        assert context.game_in_series == 2
        assert context.is_conditional_game is False

    def test_series_score_is_carried_through(self) -> None:
        context = _series_context(live_game(radiant_wins=2, dire_wins=1), SeriesFormat.BO5)
        assert (context.radiant_series_wins, context.dire_series_wins) == (2, 1)


class TestFeedEntry:
    def _entry(
        self,
        *,
        known: bool,
        fmt: SeriesFormat | None = SeriesFormat.BO3,
        tier: str = "tier1",
        valve_tier: str | None = "professional",
    ) -> dict[str, Any]:
        game = live_game(radiant_wins=1, dire_wins=0)
        return _feed_entry(
            game,
            p_radiant=0.62,
            model_version="baseline-logistic-0.1",
            minute=20,
            league_name="DreamLeague Season 29",
            tier=tier,
            series=_series_context(game, fmt),
            series_format_known=known,
            team_history=0,
            valve_tier=valve_tier,
        )

    def test_known_format_is_reported(self) -> None:
        assert self._entry(known=True)["series"]["format"] == "bo3"

    def test_unknown_format_is_null_not_a_guess(self) -> None:
        """Null so the UI shows no badge at all, rather than a confident "Bo1"."""
        assert self._entry(known=False, fmt=None)["series"]["format"] is None

    def test_broadcast_delay_survives_to_the_ui(self) -> None:
        """Required by spec section 7.4: our numbers run ahead of the stream, and saying so
        is what stops the product reading as a spoiler."""
        assert self._entry(known=True)["stream_delay_s"] == 300

    def test_duration_becomes_whole_seconds(self) -> None:
        """Valve sends a float here."""
        assert self._entry(known=True)["game_time"] == 1230

    def test_teams_and_score_are_carried(self) -> None:
        entry = self._entry(known=True)
        assert entry["radiant"]["name"] == "Radiant Side"
        assert (entry["radiant_score"], entry["dire_score"]) == (12, 7)

    @pytest.mark.parametrize(
        "field", ["match_id", "league_id", "tier", "p_radiant", "minute", "valve_tier"]
    )
    def test_identifying_fields_are_present(self, field: str) -> None:
        assert field in self._entry(known=True)

    def test_carries_the_valve_tier_for_the_api_gate(self) -> None:
        assert self._entry(known=True, valve_tier="excluded")["valve_tier"] == "excluded"

    def test_an_unmapped_premium_league_reads_as_tier1(self) -> None:
        # Section 3 fallback: otherwise the next International hides under "unmarked".
        entry = self._entry(known=True, tier="unknown", valve_tier="premium")
        assert entry["tier"] == "tier1"

    def test_liquipedia_tier_wins_when_there_is_one(self) -> None:
        assert self._entry(known=True, tier="tier3", valve_tier="premium")["tier"] == "tier3"


class TestValveTierRequest:
    def test_names_the_leagues_without_a_valve_tier(self) -> None:
        contexts = {
            1: UNKNOWN_LEAGUE._replace(valve_tier="professional"),
            2: UNKNOWN_LEAGUE,
        }
        # 3 has no context at all: the poller registers it, but a missing entry still counts.
        assert leagues_without_valve_tier(contexts, {1, 2, 3}) == {2, 3}

    def test_nothing_to_ask_when_every_league_is_known(self) -> None:
        contexts = {1: UNKNOWN_LEAGUE._replace(valve_tier="excluded")}
        assert leagues_without_valve_tier(contexts, {1}) == set()


class TestPredictionRow:
    def test_records_the_league_and_its_valve_tier_at_serve_time(self) -> None:
        context = LeagueContext(
            name="DreamLeague Season 29",
            tier="tier1",
            fmt=None,
            is_lan=None,
            valve_tier="professional",
        )
        row = prediction_row(
            match_id=8968034437,
            minute=20,
            captured_at=datetime(2026, 9, 12, tzinfo=UTC),
            model_version="lgbm-20260901-102407",
            p_radiant=0.62,
            features={"minute": 20.0},
            league_id=19696,
            context=context,
        )
        assert (row.league_id, row.valve_tier) == (19696, "professional")
        assert (row.match_id, row.minute, row.p_radiant) == (8968034437, 20, 0.62)

    def test_league_zero_is_stored_as_unknown(self) -> None:
        row = prediction_row(
            match_id=1,
            minute=1,
            captured_at=datetime(2026, 9, 12, tzinfo=UTC),
            model_version="v",
            p_radiant=0.5,
            features={},
            league_id=0,
            context=UNKNOWN_LEAGUE,
        )
        assert row.league_id is None
        assert row.valve_tier is None


class TestLeagueContext:
    async def test_reads_the_valve_tier(self, session: AsyncSession) -> None:
        session.add(League(league_id=19696, name="DreamLeague Season 29", valve_tier="premium"))
        await session.flush()

        contexts = await _league_context(session, {19696})

        assert contexts[19696].valve_tier == "premium"
        assert contexts[19696].name == "DreamLeague Season 29"
