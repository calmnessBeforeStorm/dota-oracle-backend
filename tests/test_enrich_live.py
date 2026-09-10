"""Filling gaps in the normalized layer from the poller's own snapshots (spec section 4.2).

A match first learned about through `resolve-outcomes` arrives with team ids and nothing
else: `/proMatches` covers 13 of the 448 matches we have predicted, so the summary layer
never fills these in and never will. The poller saw all 448 of them and wrote down the
league and, where Valve gave one, the team names.

The rule that makes this safe: **only gaps are filled**. A summary is a better source than
a live scoreboard sampled mid-fight, so anything already recorded wins.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.matches import Match
from app.db.models.raw import RawLiveSnapshot
from app.db.models.reference import League, Team
from app.ingestion.enrich_live import enrich_from_snapshots
from app.ingestion.reference import refresh_league_names

BASE = datetime(2026, 9, 1, tzinfo=UTC)


class _FakeLeagues:
    """`/leagues` as OpenDota returns it: every league it knows, not only ours."""

    def __init__(self, *, nameless: bool = False) -> None:
        self._nameless = nameless

    async def leagues(self) -> list[dict[str, Any]]:
        if self._nameless:
            return [{"leagueid": 17599, "name": None}]
        return [
            {"leagueid": 17599, "name": "Ultras Dota Pro League 2025-26"},
            {"leagueid": 18445, "name": "Destiny League"},
        ]


class _Once:
    """Hands the test's own session to code that expects a session factory."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def __aenter__(self) -> AsyncSession:
        return self._session

    async def __aexit__(self, *_: object) -> None:
        return None


def snapshot(
    *,
    league_id: int | None = 17599,
    radiant: tuple[int, str] | None = (7917893, "InterActive Philippines"),
    dire: tuple[int, str] | None = (36, "Team Spirit"),
) -> dict[str, Any]:
    payload: dict[str, Any] = {"league_id": league_id}
    if radiant:
        payload["radiant_team"] = {"team_id": radiant[0], "team_name": radiant[1]}
    if dire:
        payload["dire_team"] = {"team_id": dire[0], "team_name": dire[1]}
    return payload


async def add(
    session: AsyncSession,
    match_id: int,
    payload: dict[str, Any],
    *,
    league_id: int | None = None,
    captured: datetime = BASE,
) -> None:
    session.add(Match(match_id=match_id, radiant_win=True, start_time=BASE, league_id=league_id))
    session.add(RawLiveSnapshot(match_id=match_id, captured_at=captured, payload=payload))
    await session.flush()


class TestTeams:
    async def test_names_a_team_we_had_never_recorded(self, session: AsyncSession) -> None:
        await add(session, 1, snapshot())
        await enrich_from_snapshots(session)
        names = dict((await session.execute(select(Team.team_id, Team.name))).all())
        assert names[7917893] == "InterActive Philippines"
        assert names[36] == "Team Spirit"

    async def test_leaves_a_recorded_name_alone(self, session: AsyncSession) -> None:
        # The summary layer owns this field; a scoreboard must not overwrite it.
        session.add(Team(team_id=36, name="Team Spirit (official)"))
        await session.flush()
        await add(session, 1, snapshot())
        await enrich_from_snapshots(session)
        name = await session.scalar(select(Team.name).where(Team.team_id == 36))
        assert name == "Team Spirit (official)"

    async def test_skips_ad_hoc_lineups(self, session: AsyncSession) -> None:
        # Valve hands out negative ids to teams assembled for one game. They are not teams,
        # and a row for one would be a name nobody can look up.
        await add(session, 1, snapshot(radiant=(-29924145, "some stack"), dire=None))
        await enrich_from_snapshots(session)
        assert (await session.scalars(select(Team.team_id))).all() == []

    async def test_ignores_a_nameless_side(self, session: AsyncSession) -> None:
        await add(session, 1, snapshot(radiant=None, dire=None))
        await enrich_from_snapshots(session)
        assert (await session.scalars(select(Team.team_id))).all() == []


class TestLeague:
    async def test_fills_a_missing_league_id(self, session: AsyncSession) -> None:
        await add(session, 1, snapshot())
        await enrich_from_snapshots(session)
        assert await session.scalar(select(Match.league_id).where(Match.match_id == 1)) == 17599

    async def test_creates_the_league_row_it_points_at(self, session: AsyncSession) -> None:
        # `matches.league_id` is a foreign key: pointing at a league we hold no row for
        # would fail the write outright.
        await add(session, 1, snapshot())
        await enrich_from_snapshots(session)
        league = (
            await session.execute(select(League).where(League.league_id == 17599))
        ).scalar_one()
        # Valve's live scoreboard carries no league name, and inventing one is worse than
        # leaving it blank - the card says "Лига 17599" and that is the truth.
        assert league.name is None
        assert league.tier == "unknown"

    async def test_leaves_a_recorded_league_alone(self, session: AsyncSession) -> None:
        session.add(League(league_id=999, name="The International 2026"))
        await session.flush()
        await add(session, 1, snapshot(), league_id=999)
        await enrich_from_snapshots(session)
        assert await session.scalar(select(Match.league_id).where(Match.match_id == 1)) == 999

    async def test_does_not_invent_a_league_from_a_silent_snapshot(
        self, session: AsyncSession
    ) -> None:
        await add(session, 1, snapshot(league_id=None))
        await enrich_from_snapshots(session)
        assert await session.scalar(select(Match.league_id).where(Match.match_id == 1)) is None


class TestSnapshotChoice:
    async def test_uses_the_latest_snapshot(self, session: AsyncSession) -> None:
        # Early in a match Valve often reports one side and fills the other in later.
        session.add(Match(match_id=1, radiant_win=True, start_time=BASE))
        session.add(
            RawLiveSnapshot(match_id=1, captured_at=BASE, payload=snapshot(radiant=None, dire=None))
        )
        session.add(
            RawLiveSnapshot(match_id=1, captured_at=BASE + timedelta(minutes=5), payload=snapshot())
        )
        await session.flush()
        await enrich_from_snapshots(session)
        names = dict((await session.execute(select(Team.team_id, Team.name))).all())
        assert names[7917893] == "InterActive Philippines"


class TestReport:
    async def test_counts_what_it_changed(self, session: AsyncSession) -> None:
        await add(session, 1, snapshot())
        report = await enrich_from_snapshots(session)
        assert report.teams == 2
        assert report.matches == 1

    async def test_reports_nothing_on_an_empty_database(self, session: AsyncSession) -> None:
        report = await enrich_from_snapshots(session)
        assert report.teams == 0
        assert report.matches == 0


class TestLeagueNames:
    """`/leagues` is one call for the whole reference book, and nothing was calling it.

    Valve's live scoreboard gives an id and no name, so without this the feed reads
    "Лига 17599" for every tournament it covers.
    """

    async def test_names_leagues_it_already_holds(self, session: AsyncSession) -> None:
        session.add(League(league_id=17599))
        await session.flush()
        written = await refresh_league_names(_FakeLeagues(), lambda: _Once(session))
        assert written == 2
        name = await session.scalar(select(League.name).where(League.league_id == 17599))
        assert name == "Ultras Dota Pro League 2025-26"

    async def test_does_not_overwrite_a_liquipedia_tier(self, session: AsyncSession) -> None:
        # `map-leagues` owns the tier; this pass only carries names.
        session.add(League(league_id=17599, tier="tier1"))
        await session.flush()
        await refresh_league_names(_FakeLeagues(), lambda: _Once(session))
        tier = await session.scalar(select(League.tier).where(League.league_id == 17599))
        assert tier == "tier1"

    async def test_does_not_disturb_liquipedia_columns(self, session: AsyncSession) -> None:
        # The classification is hand-checked work; only `name` may be refreshed here.
        session.add(
            League(league_id=17599, tier="tier1", liquipedia_slug="Ultras/2025-26", is_lan=True)
        )
        await session.flush()
        await refresh_league_names(_FakeLeagues(), lambda: _Once(session))
        league = (
            await session.execute(select(League).where(League.league_id == 17599))
        ).scalar_one()
        assert league.name == "Ultras Dota Pro League 2025-26"
        assert league.tier == "tier1"
        assert league.liquipedia_slug == "Ultras/2025-26"
        assert league.is_lan is True

    async def test_skips_a_league_with_no_name(self, session: AsyncSession) -> None:
        written = await refresh_league_names(_FakeLeagues(nameless=True), lambda: _Once(session))
        assert written == 0
