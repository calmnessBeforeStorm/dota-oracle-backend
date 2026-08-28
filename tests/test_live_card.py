"""The match card while the game is still being played (F2, spec section 8.1).

The card used to answer "the curve is all we have" for a live match, which put two anonymous
sides and a 0:0 on the page for the entire game - the one window where a viewer is most
likely to be looking. Everything it needs is in the poller's own payloads.
"""

from datetime import UTC, datetime
from typing import Any, ClassVar

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.live_card import (
    draft_from,
    kill_score,
    latest_snapshot,
    map_score,
    players_from,
    series_from,
    stream_delay_seconds,
    teams_from,
)
from app.db.models.raw import RawLiveSnapshot
from app.db.models.reference import Hero, Player
from app.ingestion.sources import RawSource
from app.schemas.common import MatchPlayerBrief

BASE = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
MATCH = 9_000_000_001


def side(score: int, players: list[dict[str, Any]], picks: list[int], bans: list[int]) -> dict:
    return {
        "score": score,
        "tower_state": 2047,
        "barracks_state": 63,
        "players": players,
        "picks": [{"hero_id": h} for h in picks],
        "bans": [{"hero_id": h} for h in bans],
    }


def player(slot: int, hero_id: int, **extra: Any) -> dict[str, Any]:
    return {
        "player_slot": slot,
        "hero_id": hero_id,
        "account_id": 100 + slot,
        "kills": 3,
        # Singular, and only in this payload.
        "death": 1,
        "assists": 7,
        "last_hits": 120,
        "denies": 4,
        "net_worth": 12000,
        "gold_per_min": 500,
        "xp_per_min": 600,
        "level": 15,
        **extra,
    }


PAYLOAD: dict[str, Any] = {
    "match_id": MATCH,
    "league_id": 17_924,
    "stream_delay_s": 120,
    "radiant_series_wins": 1,
    "dire_series_wins": 0,
    "radiant_team": {"team_id": 11, "team_name": "Azure Dragons"},
    "dire_team": {"team_id": 22, "team_name": "Stormriders"},
    "scoreboard": {
        "duration": 1670.0,
        "radiant": side(18, [player(i, 80 + i) for i in range(5)], [80, 81], [90, 91]),
        "dire": side(12, [player(i, 60 + i) for i in range(5)], [60, 61], [70]),
    },
}


class TestReadingTheSnapshot:
    async def test_the_newest_snapshot_wins(self, session: AsyncSession) -> None:
        """One row every thirty seconds, and only the last one describes the game now."""
        for minute, score in ((0, 0), (10, 9), (20, 18)):
            session.add(
                RawLiveSnapshot(
                    match_id=MATCH,
                    source=str(RawSource.STEAM_LIVE_LEAGUE_GAMES),
                    captured_at=BASE.replace(minute=minute),
                    payload={
                        **PAYLOAD,
                        "scoreboard": {**PAYLOAD["scoreboard"], "radiant": side(score, [], [], [])},
                    },
                )
            )
        await session.flush()

        found = await latest_snapshot(session, MATCH)

        assert found is not None
        assert kill_score(found)[0] == 18

    async def test_a_match_never_seen_live_has_none(self, session: AsyncSession) -> None:
        assert await latest_snapshot(session, 12345) is None


class TestTeamsAndScore:
    def test_team_names_come_through(self) -> None:
        radiant, dire = teams_from(PAYLOAD)

        assert (radiant.name, radiant.team_id) == ("Azure Dragons", 11)
        assert (dire.name, dire.team_id) == ("Stormriders", 22)

    def test_a_missing_team_is_left_empty_not_invented(self) -> None:
        radiant, dire = teams_from({"radiant_team": {"team_id": 11, "team_name": "Alpha"}})

        assert radiant.name == "Alpha"
        assert dire.name is None

    def test_the_kill_score_is_read_from_the_scoreboard(self) -> None:
        assert kill_score(PAYLOAD) == (18, 12)

    def test_the_stream_delay_is_reported_rather_than_assumed(self) -> None:
        """Section 7.4: our numbers run ahead of the broadcast, and how far is the difference
        between a useful notice and a spoiler."""
        assert stream_delay_seconds(PAYLOAD) == 120

    def test_a_missing_delay_is_zero_which_the_notice_hides(self) -> None:
        assert stream_delay_seconds({"match_id": 1}) == 0


class TestSeries:
    def test_the_standing_comes_from_the_live_entry(self) -> None:
        series = series_from(PAYLOAD, series_format=None)

        assert (series.score_a, series.score_b) == (1, 0)

    def test_the_map_number_is_the_maps_already_played_plus_this_one(self) -> None:
        """The only place it can come from while the series is still being played."""
        assert series_from(PAYLOAD, series_format=None).game_in_series == 2

    def test_the_format_is_never_guessed(self) -> None:
        """`series_type` is in the payload and cannot express Bo2, so it is not read. A
        wrong format is worse than none: it drives the score label and the draw rule
        (spec section 5.5)."""
        assert series_from(PAYLOAD, series_format=None).format is None

    def test_a_live_series_is_never_reported_as_decided(self) -> None:
        series = series_from(PAYLOAD, series_format=None)

        assert series.winner_team_id is None
        assert series.is_draw is False


class TestDraft:
    async def test_picks_and_bans_from_both_sides(self, session: AsyncSession) -> None:
        entries = await draft_from(session, PAYLOAD)

        assert sum(1 for e in entries if e.is_pick) == 4
        assert sum(1 for e in entries if not e.is_pick) == 3

    async def test_hero_names_are_resolved(self, session: AsyncSession) -> None:
        """Without them the strip renders a row of hero ids, which is a number nobody reads
        as a hero - exactly how it looked before this was added."""
        session.add(Hero(hero_id=80, name="npc_dota_hero_lone_druid", localized_name="Lone Druid"))
        await session.flush()

        entries = await draft_from(session, PAYLOAD)

        assert any(e.hero_name == "Lone Druid" for e in entries)

    async def test_an_unknown_hero_keeps_its_id_rather_than_vanishing(
        self, session: AsyncSession
    ) -> None:
        entries = await draft_from(session, PAYLOAD)

        assert all(e.hero_id for e in entries)
        assert all(e.hero_name is None for e in entries)

    async def test_an_empty_draft_is_empty(self, session: AsyncSession) -> None:
        assert await draft_from(session, {"scoreboard": {}}) == []


class TestRosters:
    async def test_both_sides_are_returned(self, session: AsyncSession) -> None:
        players = await players_from(session, PAYLOAD)

        assert len(players) == 10
        assert sum(1 for p in players if p.is_radiant) == 5

    async def test_dire_slots_do_not_collide_with_radiant(self, session: AsyncSession) -> None:
        """The live scoreboard restarts slot numbering at zero for each side. The card keys
        rows on the slot, so without the offset five rows would disappear."""
        players = await players_from(session, PAYLOAD)

        assert len({p.player_slot for p in players}) == 10
        assert all(p.player_slot >= 128 for p in players if not p.is_radiant)

    async def test_deaths_are_read_from_the_singular_field(self, session: AsyncSession) -> None:
        """Valve spells it `death` here and `deaths` everywhere else. Reading the wrong one
        gives a roster where nobody has ever died."""
        players = await players_from(session, PAYLOAD)

        assert all(p.deaths == 1 for p in players)

    async def test_player_names_are_resolved_when_known(self, session: AsyncSession) -> None:
        session.add(Player(account_id=100, name="Dendi"))
        await session.flush()

        players = await players_from(session, PAYLOAD)

        assert any(p.player_name == "Dendi" for p in players)

    async def test_an_unknown_player_stays_nameless(self, session: AsyncSession) -> None:
        """`/proPlayers` only lists players with a pro profile, so a Tier-2 roster can come
        back entirely nameless. Null, never the account id - a number reads as a name."""
        players = await players_from(session, PAYLOAD)

        assert all(p.player_name is None for p in players)
        assert all(p.account_id is not None for p in players)

    async def test_an_empty_scoreboard_yields_no_players(self, session: AsyncSession) -> None:
        assert await players_from(session, {"scoreboard": {}}) == []


class TestMapScore:
    """Which source owns the kill score, and when.

    Written after getting it backwards: routing every match through the live snapshot made a
    finished game report 52:48 while its own rosters said 51:53 - the snapshot was taken
    thirty seconds before the ancient fell, and that is what it will always be.
    """

    ROSTERS: ClassVar[list[MatchPlayerBrief]] = [
        MatchPlayerBrief(player_slot=0, is_radiant=True, kills=30),
        MatchPlayerBrief(player_slot=1, is_radiant=True, kills=21),
        MatchPlayerBrief(player_slot=128, is_radiant=False, kills=53),
    ]

    def test_a_finished_match_is_scored_from_its_rosters(self) -> None:
        assert map_score(self.ROSTERS, PAYLOAD, is_live=False) == (51, 53)

    def test_a_live_match_is_scored_from_the_scoreboard(self) -> None:
        """The other way round while it is running: rosters, if they exist at all, are older
        than the scoreboard."""
        assert map_score(self.ROSTERS, PAYLOAD, is_live=True) == (18, 12)

    def test_a_live_match_with_no_snapshot_falls_back_to_rosters(self) -> None:
        assert map_score(self.ROSTERS, None, is_live=True) == (51, 53)

    def test_a_match_with_neither_has_no_score(self) -> None:
        """Null, not zero: 0:0 is a real score a match can have."""
        assert map_score([], None, is_live=False) == (None, None)

    def test_a_missing_kill_count_does_not_break_the_sum(self) -> None:
        rosters = [
            MatchPlayerBrief(player_slot=0, is_radiant=True, kills=None),
            MatchPlayerBrief(player_slot=1, is_radiant=True, kills=4),
        ]

        assert map_score(rosters, None, is_live=False) == (4, 0)
