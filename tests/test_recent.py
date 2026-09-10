"""The home screen's feed of finished matches (spec section 8.1).

The home screen is built around a single number, and for most of the day that number does
not exist: Tier 1 matches run a few hours a day. This feed is what the screen shows the
rest of the time, which makes it as bound to honesty as the accuracy dashboard is.
"""

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.recent import pick_tenth_minute, recent_matches, thin
from app.api.routes.matches import router as matches_router
from app.db.models.matches import Match
from app.db.models.training import Prediction
from app.schemas.common import PredictionPoint

BASE = datetime(2026, 9, 1, tzinfo=UTC)
VERSION = "lgbm-20260901-090724"


def point(minute: int, p: float = 0.5) -> PredictionPoint:
    return PredictionPoint(minute=minute, p_radiant=p, predicted_at=BASE)


async def add(
    session: AsyncSession,
    match_id: int,
    radiant_win: bool | None,
    minutes: list[int],
    *,
    start: datetime = BASE,
) -> None:
    session.add(Match(match_id=match_id, radiant_win=radiant_win, start_time=start))
    await session.flush()
    for minute in minutes:
        session.add(
            Prediction(
                match_id=match_id,
                minute=minute,
                predicted_at=start + timedelta(minutes=minute),
                model_version=VERSION,
                p_radiant=0.5 + minute / 200,
                features={},
            )
        )
    await session.flush()


class TestTenthMinute:
    def test_takes_the_first_point_at_or_after_ten(self) -> None:
        picked = pick_tenth_minute([point(4), point(11, 0.62), point(20)])
        assert picked is not None
        assert picked.p_radiant == 0.62

    def test_dashes_when_the_minute_is_missing(self) -> None:
        # Passing minute 22 off as "minute ten" is impossible to spot from the outside.
        assert pick_tenth_minute([point(2), point(22)]) is None

    def test_dashes_on_an_empty_curve(self) -> None:
        assert pick_tenth_minute([]) is None

    def test_accepts_the_far_edge_of_the_bucket(self) -> None:
        picked = pick_tenth_minute([point(14, 0.7)])
        assert picked is not None
        assert picked.minute == 14


class TestThin:
    def test_keeps_a_short_curve_whole(self) -> None:
        curve = [point(m) for m in range(5)]
        assert thin(curve, target=30) == curve

    def test_keeps_the_ends(self) -> None:
        curve = [point(m) for m in range(100)]
        thinned = thin(curve, target=30)
        assert len(thinned) <= 30
        assert thinned[0].minute == 0
        assert thinned[-1].minute == 99


class TestRecentMatches:
    async def test_returns_only_finished_matches(self, session: AsyncSession) -> None:
        await add(session, 1, True, [0, 10, 20])
        await add(session, 2, None, [0, 10])
        rows = await recent_matches(session)
        assert [row.match_id for row in rows] == [1]

    async def test_skips_matches_nobody_predicted(self, session: AsyncSession) -> None:
        # The feed's population is what the live loop saw, not the whole match archive.
        session.add(Match(match_id=3, radiant_win=True, start_time=BASE))
        await session.flush()
        assert await recent_matches(session) == []

    async def test_newest_first(self, session: AsyncSession) -> None:
        await add(session, 1, True, [10], start=BASE)
        await add(session, 2, False, [10], start=BASE + timedelta(days=1))
        rows = await recent_matches(session)
        assert [row.match_id for row in rows] == [2, 1]

    async def test_carries_the_curve_and_the_tenth_minute(self, session: AsyncSession) -> None:
        await add(session, 1, True, [0, 10, 20])
        row = (await recent_matches(session))[0]
        assert [p.minute for p in row.curve] == [0, 10, 20]
        assert row.p_at_ten == 0.55

    async def test_names_the_version_behind_the_tenth_minute(self, session: AsyncSession) -> None:
        # A match can be predicted by two versions in a row; the card names the one that
        # made this very claim, not "the match's version", which does not exist.
        await add(session, 1, True, [0, 10])
        row = (await recent_matches(session))[0]
        assert row.model_version == VERSION

    async def test_leaves_the_version_empty_without_a_tenth_minute(
        self, session: AsyncSession
    ) -> None:
        await add(session, 1, True, [0, 30])
        row = (await recent_matches(session))[0]
        assert row.p_at_ten is None
        assert row.model_version == ""

    async def test_honours_the_limit(self, session: AsyncSession) -> None:
        for match_id in range(1, 6):
            await add(session, match_id, True, [10], start=BASE + timedelta(hours=match_id))
        assert len(await recent_matches(session, limit=3)) == 3


class TestRoute:
    def test_route_is_registered(self, client: TestClient) -> None:
        paths = client.get("/openapi.json").json()["paths"]
        assert "/api/matches/recent" in paths

    def test_recent_is_matched_before_the_id_route(self) -> None:
        # Otherwise `recent` is swallowed by `/{match_id}` as a match id. Checked by the
        # order inside the router rather than by a request: a request would hit the live
        # database. And not via `app.routes` - included routers are not expanded there, so
        # that slice would quietly come back empty and the test would prove anything.
        paths = [getattr(route, "path", "") for route in matches_router.routes]
        assert paths.index("/matches/recent") < paths.index("/matches/{match_id}")

    def test_refuses_an_unreasonable_limit(self, client: TestClient) -> None:
        # This is a home-screen feed; fifty cards is the ceiling, beyond it the endpoint
        # is an archive dump. Validation rejects the request before a session is touched.
        assert client.get("/api/matches/recent", params={"limit": 500}).status_code == 422
