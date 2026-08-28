"""Resolving outcomes for predictions we have already served (spec sections 4.3, 8.1).

Invariant 8 logs every prediction; this is the other half of the loop. The failure it
guards against is silent by construction: an unscored prediction looks exactly like a
dashboard that has not been given enough time yet.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.matches import Match
from app.db.models.raw import RawMatch
from app.db.models.training import Prediction
from app.ingestion.repository import upsert_raw_matches
from app.ingestion.sources import RawSource
from app.ingestion.workers.outcomes import (
    count_unresolved_predictions,
    resolve_outcomes,
    select_unresolved_predictions,
)

BASE = datetime(2026, 8, 1, tzinfo=UTC)


class FakeStratz:
    """Answers per id, the way `match(id:)` does."""

    def __init__(self, known: dict[int, bool] | None = None) -> None:
        self.known = known or {}
        self.asked: list[int] = []

    async def match(self, match_id: int) -> dict[str, Any]:
        self.asked.append(match_id)
        return {
            "id": match_id,
            "didRadiantWin": self.known.get(match_id, True),
            "startDateTime": int(BASE.timestamp()),
            "durationSeconds": 2000,
            "radiantTeamId": 1,
            "direTeamId": 2,
            "players": [],
            "pickBans": [],
            "towerDeaths": [],
        }


async def add_prediction(session: AsyncSession, match_id: int, minute: int = 10) -> None:
    session.add(
        Prediction(
            match_id=match_id,
            minute=minute,
            predicted_at=BASE + timedelta(minutes=minute),
            model_version="live-v1",
            p_radiant=0.6,
            features={},
        )
    )
    await session.flush()


class TestSelection:
    async def test_a_predicted_match_we_know_nothing_about_is_selected(
        self, session: AsyncSession
    ) -> None:
        await add_prediction(session, 100)

        assert await select_unresolved_predictions(session, 10) == [100]

    async def test_a_match_with_a_known_outcome_is_not_selected(
        self, session: AsyncSession
    ) -> None:
        session.add(Match(match_id=100, radiant_win=True, start_time=BASE))
        await add_prediction(session, 100)

        assert await select_unresolved_predictions(session, 10) == []

    async def test_a_match_still_running_is_selected(self, session: AsyncSession) -> None:
        """A row in `matches` is not an outcome. The live poller's match exists long before
        anybody knows who won it."""
        session.add(Match(match_id=100, radiant_win=None, start_time=BASE))
        await add_prediction(session, 100)

        assert await select_unresolved_predictions(session, 10) == [100]

    async def test_a_match_whose_payload_we_already_hold_is_not_refetched(
        self, session: AsyncSession
    ) -> None:
        """That gap belongs to `normalize`, not to the network. Re-fetching would spend
        quota to re-learn what is already on disk."""
        await add_prediction(session, 100)
        await upsert_raw_matches(session, RawSource.STRATZ_MATCH, [{"id": 100}])

        assert await select_unresolved_predictions(session, 10) == []

    async def test_many_predictions_for_one_match_ask_once(self, session: AsyncSession) -> None:
        """A 40-minute match leaves ~80 rows in `predictions`. Fetching it 80 times would
        exhaust the hourly allowance on a single game."""
        for minute in range(40):
            await add_prediction(session, 100, minute=minute)

        assert await select_unresolved_predictions(session, 10) == [100]

    async def test_newest_first(self, session: AsyncSession) -> None:
        for match_id in (100, 300, 200):
            await add_prediction(session, match_id)

        assert await select_unresolved_predictions(session, 10) == [300, 200, 100]

    async def test_the_count_matches_the_selection(self, session: AsyncSession) -> None:
        for match_id in (100, 200, 300):
            await add_prediction(session, match_id)
        session.add(Match(match_id=300, radiant_win=False, start_time=BASE))
        await session.flush()

        assert await count_unresolved_predictions(session) == 2


class TestResolving:
    async def test_it_stores_a_payload_per_unresolved_match(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        await add_prediction(session, 100)
        await add_prediction(session, 200)
        await session.commit()
        client = FakeStratz()

        report = await resolve_outcomes(client, sessionmaker, limit=10)

        assert sorted(client.asked) == [100, 200]
        assert report.fetched == 2
        async with sessionmaker() as check:
            stored = (
                await check.execute(
                    select(RawMatch.match_id).where(RawMatch.source == str(RawSource.STRATZ_MATCH))
                )
            ).scalars()
            assert sorted(stored) == [100, 200]

    async def test_rerunning_asks_for_nothing(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        await add_prediction(session, 100)
        await session.commit()
        client = FakeStratz()
        await resolve_outcomes(client, sessionmaker, limit=10)
        client.asked.clear()

        second = await resolve_outcomes(client, sessionmaker, limit=10)

        assert client.asked == []
        assert second.requested == 0

    async def test_the_limit_is_honoured(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        for match_id in (100, 200, 300):
            await add_prediction(session, match_id)
        await session.commit()

        report = await resolve_outcomes(FakeStratz(), sessionmaker, limit=2)

        assert report.fetched == 2
        assert report.remaining == 1
