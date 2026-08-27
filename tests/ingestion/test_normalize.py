"""Normalization of /proMatches summaries (spec sections 4.2, 5.5, phase 1)."""

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.matches import Match, MatchObjective, MatchPlayer, Series
from app.db.models.reference import League, Team
from app.ingestion.normalize import (
    normalize_match_details,
    normalize_pro_matches,
    parse_match_detail,
    parse_stratz_match_detail,
    series_id_of,
    team_id_of,
)
from app.ingestion.repository import upsert_raw_matches
from app.ingestion.sources import RawSource

BASE_TIME = 1_780_000_000


def summary(
    match_id: int,
    *,
    league_id: int = 100,
    series_id: int | None = 0,
    series_type: int = 1,
    radiant_team_id: int = 1,
    dire_team_id: int = 2,
    radiant_win: bool = True,
    offset_seconds: int = 0,
    version: int | None = 21,
) -> dict[str, Any]:
    return {
        "match_id": match_id,
        "leagueid": league_id,
        "league_name": f"League {league_id}",
        "series_id": series_id,
        "series_type": series_type,
        "radiant_team_id": radiant_team_id,
        "radiant_name": f"Team {radiant_team_id}",
        "dire_team_id": dire_team_id,
        "dire_name": f"Team {dire_team_id}",
        "radiant_win": radiant_win,
        "start_time": BASE_TIME + offset_seconds,
        "duration": 2000,
        "version": version,
    }


async def load(session: AsyncSession, payloads: list[dict[str, Any]]) -> None:
    await upsert_raw_matches(session, RawSource.OPENDOTA_PRO_MATCHES, payloads)
    await session.commit()


class TestFieldParsing:
    @pytest.mark.parametrize("raw", [0, None])
    def test_missing_series_is_not_a_series(self, raw: int | None) -> None:
        """Valve sends 0 or null for a standalone map. Treating 0 as an id would fuse every
        standalone map in the dataset into one enormous fake series."""
        assert series_id_of({"series_id": raw}) is None

    def test_real_series_id_survives(self) -> None:
        assert series_id_of({"series_id": 1088695}) == 1088695

    @pytest.mark.parametrize("raw", [0, None])
    def test_unregistered_team_is_none(self, raw: int | None) -> None:
        assert team_id_of({"radiant_team_id": raw}, "radiant") is None


class TestNormalization:
    async def test_fills_reference_and_match_layers(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        await load(session, [summary(1), summary(2, radiant_team_id=3)])
        report = await normalize_pro_matches(sessionmaker)

        assert report.raw_seen == 2
        assert (await session.execute(select(League))).scalars().all()
        teams = (await session.execute(select(Team.team_id))).scalars().all()
        assert set(teams) == {1, 2, 3}

    async def test_standalone_maps_get_no_series(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        await load(session, [summary(1, series_id=0), summary(2, series_id=None)])
        await normalize_pro_matches(sessionmaker)

        matches = (await session.execute(select(Match))).scalars().all()
        assert {m.series_id for m in matches} == {None}
        assert (await session.execute(select(Series))).scalars().all() == []

    async def test_same_valve_series_id_in_two_leagues_stays_separate(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """The regression this key change exists for.

        Valve's series id is only unique inside a league. Keyed by it alone, two unrelated
        series in different leagues were fused into one - observed on real data.
        """
        await load(
            session,
            [
                summary(1, league_id=100, series_id=777),
                summary(2, league_id=200, series_id=777, radiant_team_id=5, dire_team_id=6),
            ],
        )
        await normalize_pro_matches(sessionmaker)

        series = (await session.execute(select(Series))).scalars().all()
        assert len(series) == 2
        assert {s.league_id for s in series} == {100, 200}
        assert {s.valve_series_id for s in series} == {777}

    async def test_numbers_maps_by_start_time(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        # Deliberately loaded out of order: numbering must follow the clock, not the id.
        await load(
            session,
            [
                summary(20, series_id=5, offset_seconds=3600),
                summary(10, series_id=5, offset_seconds=0),
                summary(30, series_id=5, offset_seconds=7200),
            ],
        )
        await normalize_pro_matches(sessionmaker)

        rows = (
            await session.execute(
                select(Match.match_id, Match.game_in_series).order_by(Match.match_id)
            )
        ).all()
        assert dict(rows) == {10: 1, 20: 2, 30: 3}

    async def test_series_score_counts_wins_through_side_swaps(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """Teams change sides between maps, so wins cannot be counted by side."""
        await load(
            session,
            [
                # Map 1: team 1 on radiant, wins.
                summary(1, series_id=9, radiant_team_id=1, dire_team_id=2, radiant_win=True),
                # Map 2: sides swapped, team 1 now on dire and wins again.
                summary(
                    2,
                    series_id=9,
                    radiant_team_id=2,
                    dire_team_id=1,
                    radiant_win=False,
                    offset_seconds=3600,
                ),
            ],
        )
        await normalize_pro_matches(sessionmaker)

        series = (await session.execute(select(Series))).scalar_one()
        assert series.team_a_id == 1
        assert (series.score_a, series.score_b) == (2, 0)

    async def test_format_and_conditionality_stay_unknown(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """Both come from Liquipedia in phase 2. A guess here would be indistinguishable
        from knowledge later, and is_conditional_game feeds the model directly (section 5.5)."""
        await load(session, [summary(1, series_id=9, series_type=1)])
        await normalize_pro_matches(sessionmaker)

        series = (await session.execute(select(Series))).scalar_one()
        match = (await session.execute(select(Match))).scalar_one()
        assert series.format is None
        assert series.valve_series_type == 1  # the unreliable Valve hint is still recorded
        assert match.is_conditional_game is None

    async def test_unparsed_matches_are_flagged(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        await load(session, [summary(1, version=None), summary(2, version=21)])
        await normalize_pro_matches(sessionmaker)

        rows = (
            await session.execute(select(Match.match_id, Match.is_parsed).order_by(Match.match_id))
        ).all()
        assert dict(rows) == {1: False, 2: True}

    async def test_start_time_is_utc(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        await load(session, [summary(1)])
        await normalize_pro_matches(sessionmaker)
        match = (await session.execute(select(Match))).scalar_one()
        assert match.start_time == datetime.fromtimestamp(BASE_TIME, tz=UTC)

    async def test_rerun_is_idempotent(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """The normalized layer is rebuilt from raw whenever parsing rules change, so
        re-running must converge rather than accumulate."""
        await load(session, [summary(1, series_id=9), summary(2, series_id=9, offset_seconds=60)])

        await normalize_pro_matches(sessionmaker)
        first = len((await session.execute(select(Series))).scalars().all())

        await normalize_pro_matches(sessionmaker)
        await session.commit()
        second = len((await session.execute(select(Series))).scalars().all())

        assert first == second == 1
        assert len((await session.execute(select(Match))).scalars().all()) == 2


STRATZ_PAYLOAD: dict[str, Any] = {
    "id": 42,
    "durationSeconds": 1800,
    "didRadiantWin": True,
    "parsedDateTime": 1787702910,
    "gameVersionId": 190,
    # Present in the payload and deliberately never read: series membership belongs to the
    # /proMatches summary layer (invariant 11).
    "seriesId": 777,
    "players": [
        {
            "playerSlot": 0,
            "steamAccountId": 111,
            "heroId": 8,
            "isRadiant": True,
            "kills": 5,
            "deaths": 1,
            "assists": 3,
            "numLastHits": 200,
            "numDenies": 10,
            "networth": 20000,
            "goldPerMinute": 600,
            "experiencePerMinute": 700,
            "leaverStatus": "NONE",
            "lane": "SAFE_LANE",
        },
        {
            "playerSlot": 128,
            "steamAccountId": 222,
            "heroId": 9,
            "isRadiant": False,
            "kills": 1,
            "deaths": 5,
            "assists": 0,
            "numLastHits": 100,
            "numDenies": 2,
            "networth": 12000,
            "goldPerMinute": 400,
            "experiencePerMinute": 450,
            "leaverStatus": "NONE",
            "lane": "OFF_LANE",
        },
    ],
    "pickBans": [
        {"order": 0, "isPick": False, "bannedHeroId": 14, "heroId": None, "isRadiant": True},
        {"order": 1, "isPick": True, "heroId": 8, "bannedHeroId": None, "isRadiant": True},
        {"order": 2, "isPick": True, "heroId": 9, "bannedHeroId": None, "isRadiant": False},
    ],
    "towerDeaths": [
        {"time": 600, "npcId": 16, "isRadiant": True},
        {"time": 700, "npcId": 36, "isRadiant": True},
        {"time": 900, "npcId": 45, "isRadiant": False},
        {"time": 1800, "npcId": 51, "isRadiant": False},
    ],
}


class TestParseStratzMatchDetail:
    def test_players_map_onto_the_same_columns_as_opendota(self) -> None:
        rows = parse_stratz_match_detail(STRATZ_PAYLOAD)["players"]
        assert len(rows) == 2
        radiant = next(r for r in rows if r["player_slot"] == 0)
        assert radiant["account_id"] == 111
        assert radiant["hero_id"] == 8
        assert radiant["is_radiant"] is True
        assert radiant["last_hits"] == 200
        assert radiant["denies"] == 10
        assert radiant["net_worth"] == 20000
        assert radiant["gold_per_min"] == 600
        assert radiant["xp_per_min"] == 700

    def test_both_parsers_produce_the_same_columns(self) -> None:
        """They feed the same tables through the same upserts, so a column that exists on
        one side and not the other would only surface as a database error much later."""
        stratz = parse_stratz_match_detail(STRATZ_PAYLOAD)
        opendota = parse_match_detail(
            {
                "match_id": 42,
                "players": [{"player_slot": 0, "account_id": 1, "hero_id": 2}],
                "picks_bans": [{"order": 0, "is_pick": True, "hero_id": 2, "team": 0}],
                "objectives": [{"type": "building_kill", "time": 10, "key": "x"}],
            }
        )
        for section in ("players", "drafts", "objectives"):
            assert set(stratz[section][0]) == set(opendota[section][0]), section

    def test_draft_carries_bans_and_picks(self) -> None:
        rows = parse_stratz_match_detail(STRATZ_PAYLOAD)["drafts"]
        assert {r["order"]: (r["is_pick"], r["hero_id"], r["team"]) for r in rows} == {
            0: (False, 14, 0),
            1: (True, 8, 0),
            2: (True, 9, 1),
        }

    def test_objectives_are_building_kills_named_the_opendota_way(self) -> None:
        rows = parse_stratz_match_detail(STRATZ_PAYLOAD)["objectives"]
        assert [(r["time"], r["key"], r["team"]) for r in rows] == [
            (600, "npc_dota_goodguys_tower1_top", 2),
            (900, "npc_dota_badguys_melee_rax_mid", 3),
            (1800, "npc_dota_badguys_fort", 3),
        ]
        assert [r["ordinal"] for r in rows] == [0, 1, 2]
        assert {r["type"] for r in rows} == {"building_kill"}

    def test_unknown_npc_ids_are_dropped(self) -> None:
        """36 is one of the two ids that have no counterpart in OpenDota's log."""
        rows = parse_stratz_match_detail(STRATZ_PAYLOAD)["objectives"]
        assert all(r["time"] != 700 for r in rows)

    def test_series_membership_is_never_touched(self) -> None:
        parsed = parse_stratz_match_detail(STRATZ_PAYLOAD)
        assert set(parsed) == {"players", "drafts", "objectives"}
        for rows in parsed.values():
            for row in rows:
                assert "series_id" not in row
                assert "series_type" not in row


class TestBothSourcesNormalize:
    async def test_a_stratz_payload_reaches_the_shared_tables(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        await upsert_raw_matches(session, RawSource.OPENDOTA_PRO_MATCHES, [summary(42)])
        await session.commit()
        await normalize_pro_matches(sessionmaker)

        await upsert_raw_matches(session, RawSource.STRATZ_MATCH, [STRATZ_PAYLOAD])
        await session.commit()

        report = await normalize_match_details(sessionmaker)

        assert report.match_players == 2
        assert report.match_drafts == 3
        assert report.match_objectives == 3
        async with sessionmaker() as check:
            slots = (await check.execute(select(MatchPlayer.player_slot))).scalars().all()
            assert sorted(slots) == [0, 128]
            keys = (await check.execute(select(MatchObjective.key))).scalars().all()
            assert "npc_dota_goodguys_tower1_top" in keys

    async def test_stratz_leaves_patch_null_rather_than_writing_a_foreign_scale(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """`gameVersionId` is not OpenDota's `patch`. Writing one into a column meaning the
        other is indistinguishable from knowledge later (invariant 12)."""
        await upsert_raw_matches(session, RawSource.OPENDOTA_PRO_MATCHES, [summary(42)])
        await session.commit()
        await normalize_pro_matches(sessionmaker)
        await upsert_raw_matches(session, RawSource.STRATZ_MATCH, [STRATZ_PAYLOAD])
        await session.commit()

        await normalize_match_details(sessionmaker)

        async with sessionmaker() as check:
            row = (await check.execute(select(Match).where(Match.match_id == 42))).scalar_one()
            assert row.patch is None
            assert row.is_parsed is True

    async def test_series_membership_survives_a_detail_pass(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """Invariant 11, now that the detail payload actually carries a seriesId."""
        await upsert_raw_matches(
            session, RawSource.OPENDOTA_PRO_MATCHES, [summary(42, series_id=9)]
        )
        await session.commit()
        await normalize_pro_matches(sessionmaker)
        async with sessionmaker() as check:
            before = (
                await check.execute(select(Match.series_id).where(Match.match_id == 42))
            ).scalar_one()
        assert before is not None

        await upsert_raw_matches(session, RawSource.STRATZ_MATCH, [STRATZ_PAYLOAD])
        await session.commit()
        await normalize_match_details(sessionmaker)

        async with sessionmaker() as check:
            after = (
                await check.execute(select(Match.series_id).where(Match.match_id == 42))
            ).scalar_one()
        assert after == before


class TestLargeBatches:
    async def test_more_rows_than_postgres_will_bind_in_one_statement(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """Postgres caps a statement at 32767 bind parameters, and one row spends one per
        column. A few hundred maps carry thousands of objectives between them, so the
        insert has to be chunked - this used to blow up on a real normalize run.
        """
        payloads = [
            dict(
                STRATZ_PAYLOAD,
                id=match_id,
                towerDeaths=[
                    {"time": 60 * i, "npcId": 16 + (i % 9), "isRadiant": True} for i in range(30)
                ],
            )
            for match_id in range(1000, 1300)
        ]
        await upsert_raw_matches(
            session,
            RawSource.OPENDOTA_PRO_MATCHES,
            [summary(p["id"]) for p in payloads],
        )
        await session.commit()
        await normalize_pro_matches(sessionmaker)
        await upsert_raw_matches(session, RawSource.STRATZ_MATCH, payloads)
        await session.commit()

        report = await normalize_match_details(sessionmaker)

        assert report.match_objectives == 300 * 30
        async with sessionmaker() as check:
            stored = (
                await check.execute(select(func.count()).select_from(MatchObjective))
            ).scalar_one()
        assert stored == 300 * 30


class TestSourcePrecedence:
    async def test_a_map_held_by_both_providers_is_normalized_once_from_stratz(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """Both payloads describe the same map, so feeding both into one insert makes
        Postgres refuse the statement outright. STRATZ wins because it is the source the
        snapshots are built from - the normalized tables should describe the same match.
        """
        await upsert_raw_matches(session, RawSource.OPENDOTA_PRO_MATCHES, [summary(42)])
        await session.commit()
        await normalize_pro_matches(sessionmaker)

        opendota_payload = {
            "match_id": 42,
            "version": 21,
            "patch": 60,
            "players": [
                {"player_slot": 0, "account_id": 999, "hero_id": 1},
                {"player_slot": 128, "account_id": 998, "hero_id": 2},
            ],
            "picks_bans": [],
            "objectives": [],
        }
        await upsert_raw_matches(session, RawSource.OPENDOTA_MATCH, [opendota_payload])
        await upsert_raw_matches(session, RawSource.STRATZ_MATCH, [STRATZ_PAYLOAD])
        await session.commit()

        report = await normalize_match_details(sessionmaker)

        assert report.raw_seen == 1  # one map, not one row per provider
        async with sessionmaker() as check:
            accounts = (await check.execute(select(MatchPlayer.account_id))).scalars().all()
        assert sorted(a for a in accounts if a) == [111, 222]  # the STRATZ roster

    async def test_a_map_only_opendota_has_is_still_normalized(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """The fallback matters: 685 maps were fetched from OpenDota before the switch."""
        await upsert_raw_matches(session, RawSource.OPENDOTA_PRO_MATCHES, [summary(43)])
        await session.commit()
        await normalize_pro_matches(sessionmaker)
        await upsert_raw_matches(
            session,
            RawSource.OPENDOTA_MATCH,
            [
                {
                    "match_id": 43,
                    "version": 21,
                    "patch": 60,
                    "players": [{"player_slot": 0, "account_id": 999, "hero_id": 1}],
                    "picks_bans": [],
                    "objectives": [],
                }
            ],
        )
        await session.commit()

        report = await normalize_match_details(sessionmaker)

        assert report.match_players == 1
        async with sessionmaker() as check:
            row = (await check.execute(select(Match).where(Match.match_id == 43))).scalar_one()
        assert row.patch == 60  # OpenDota can supply a patch; STRATZ cannot
