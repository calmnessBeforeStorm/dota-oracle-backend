"""Normalization of /proMatches summaries (spec sections 4.2, 5.5, phase 1)."""

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.matches import Match, Series
from app.db.models.reference import League, Team
from app.ingestion.normalize import normalize_pro_matches, series_id_of, team_id_of
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
