"""Backfill behaviour, against a real database (spec section 4.4, phase 1).

The acceptance criterion for phase 1 is not "matches are in the database", it is "restarting
does not produce duplicates". A backfill of a year of pro matches takes hours and will be
interrupted, so resumability is the feature being tested here, not a detail of it.
"""

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.raw import RawMatch
from app.ingestion.repository import count_raw_matches, get_checkpoint, upsert_raw_matches
from app.ingestion.sources import Checkpoint, RawSource
from app.ingestion.workers.backfill import run_backfill


def make_match(match_id: int, **extra: Any) -> dict[str, Any]:
    return {
        "match_id": match_id,
        "radiant_win": True,
        "league_name": "Test League",
        "series_id": 0,
        "series_type": 0,
        **extra,
    }


class FakeOpenDota:
    """Pages backwards through a fixed history, the way /proMatches does."""

    def __init__(self, match_ids: list[int], page_size: int = 3) -> None:
        self.history = sorted(match_ids, reverse=True)
        self.page_size = page_size
        self.calls: list[int | None] = []

    async def pro_matches(self, less_than_match_id: int | None = None) -> list[dict[str, Any]]:
        self.calls.append(less_than_match_id)
        available = [
            m for m in self.history if less_than_match_id is None or m < less_than_match_id
        ]
        return [make_match(m) for m in available[: self.page_size]]


class TestUpsert:
    async def test_writes_payloads(self, session: AsyncSession) -> None:
        written = await upsert_raw_matches(
            session, RawSource.OPENDOTA_PRO_MATCHES, [make_match(1), make_match(2)]
        )
        await session.commit()
        assert written == 2
        assert await count_raw_matches(session) == 2

    async def test_second_write_updates_in_place(self, session: AsyncSession) -> None:
        await upsert_raw_matches(session, RawSource.OPENDOTA_PRO_MATCHES, [make_match(1)])
        await session.commit()

        await upsert_raw_matches(
            session, RawSource.OPENDOTA_PRO_MATCHES, [make_match(1, league_name="Renamed")]
        )
        await session.commit()

        assert await count_raw_matches(session) == 1
        row = (await session.execute(select(RawMatch))).scalar_one()
        assert row.payload["league_name"] == "Renamed"

    async def test_same_match_from_two_sources_coexists(self, session: AsyncSession) -> None:
        """The /proMatches summary and the full match payload must not clobber each other -
        each cost quota to fetch and neither can be derived from the other."""
        await upsert_raw_matches(session, RawSource.OPENDOTA_PRO_MATCHES, [make_match(1)])
        await upsert_raw_matches(session, RawSource.OPENDOTA_MATCH, [make_match(1)])
        await session.commit()
        assert await count_raw_matches(session) == 2

    async def test_ignores_payloads_without_match_id(self, session: AsyncSession) -> None:
        written = await upsert_raw_matches(
            session, RawSource.OPENDOTA_PRO_MATCHES, [{"league_name": "no id"}]
        )
        assert written == 0


class TestBackfill:
    async def test_walks_backwards_and_stores(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        client = FakeOpenDota([10, 9, 8, 7, 6, 5], page_size=3)
        report = await run_backfill(client, sessionmaker, pages=2)

        assert report.pages == 2
        assert report.rows == 6
        assert report.lowest_match_id == 5
        # First call unbounded, second bounded by the lowest id of the first page.
        assert client.calls == [None, 8]

        async with sessionmaker() as session:
            assert await count_raw_matches(session) == 6

    async def test_rerun_produces_no_duplicates(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """Phase 1 acceptance criterion, stated directly."""
        history = [10, 9, 8, 7, 6, 5]
        await run_backfill(FakeOpenDota(history), sessionmaker, pages=2)
        async with sessionmaker() as session:
            after_first = await count_raw_matches(session)

        await run_backfill(FakeOpenDota(history), sessionmaker, pages=2, restart=True)
        async with sessionmaker() as session:
            assert await count_raw_matches(session) == after_first

    async def test_resumes_from_the_checkpoint(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        history = [10, 9, 8, 7, 6, 5]
        await run_backfill(FakeOpenDota(history), sessionmaker, pages=1)

        second = FakeOpenDota(history)
        await run_backfill(second, sessionmaker, pages=1)

        # The interrupted run left the cursor at 8, so the resumed one asks for older only.
        assert second.calls == [8]
        async with sessionmaker() as session:
            assert await get_checkpoint(session, Checkpoint.OPENDOTA_PRO_MATCHES) == "5"

    async def test_restart_ignores_the_checkpoint(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        history = [10, 9, 8, 7, 6, 5]
        await run_backfill(FakeOpenDota(history), sessionmaker, pages=1)

        second = FakeOpenDota(history)
        await run_backfill(second, sessionmaker, pages=1, restart=True)
        assert second.calls == [None]

    async def test_stops_on_an_empty_page(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """History runs out long before the requested page count - that must end the run,
        not spin through the remaining pages burning quota."""
        client = FakeOpenDota([10, 9], page_size=3)
        report = await run_backfill(client, sessionmaker, pages=5)

        assert report.pages == 1
        assert report.rows == 2
        assert report.stopped_because == "upstream returned an empty page"
        assert len(client.calls) == 2


@pytest.mark.parametrize("pages", [0, 1, 3])
async def test_page_count_is_honoured(
    sessionmaker: async_sessionmaker[AsyncSession], pages: int
) -> None:
    client = FakeOpenDota(list(range(100, 50, -1)), page_size=3)
    report = await run_backfill(client, sessionmaker, pages=pages)
    assert report.pages == pages
    assert len(client.calls) == pages
