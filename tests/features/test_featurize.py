"""Snapshot building from stored payloads (spec sections 5.1, 5.3, phase 3).

`featurize` reads STRATZ payloads, not OpenDota ones. The two report different quantities in
their per-minute series - earned gold against net worth - so the training set is built from
one of them and only one; see
docs/superpowers/specs/2026-08-27-stratz-adapter-design.md.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.matches import Match
from app.db.models.training import MatchSnapshot
from app.features.featurize import featurize
from app.features.live import FEATURE_ORDER
from app.ingestion.repository import upsert_raw_matches
from app.ingestion.sources import RawSource

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "stratz" / "match_8946228708.json"


def stratz_match() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def stub(match_id: int, **overrides: Any) -> dict[str, Any]:
    """A payload with the shape featurize inspects and nothing else."""
    return {
        "id": match_id,
        "parsedDateTime": 1787702910,
        "radiantNetworthLeads": [0, 1, 2],
        "didRadiantWin": True,
        "durationSeconds": 1800,
        "players": [],
        "towerDeaths": [],
        **overrides,
    }


async def seed(session: AsyncSession, payloads: list[dict[str, Any]]) -> None:
    session.add_all(
        [
            Match(match_id=int(p["id"]), start_time=datetime(2026, 8, 1, tzinfo=UTC))
            for p in payloads
        ]
    )
    await session.commit()
    await upsert_raw_matches(session, RawSource.STRATZ_MATCH, payloads)
    await session.commit()


class TestSnapshotsFromStratz:
    async def test_a_real_match_becomes_one_row_per_minute(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        match = stratz_match()
        await seed(session, [match])

        report = await featurize(sessionmaker)

        minutes = match["durationSeconds"] // 60 + 1
        assert report.matches_used == 1
        assert report.snapshots == minutes
        async with sessionmaker() as check:
            rows = list((await check.execute(select(MatchSnapshot))).scalars().all())
        assert {r.minute for r in rows} == set(range(minutes))
        assert all(set(r.features) == set(FEATURE_ORDER) for r in rows)
        assert all(r.radiant_win is match["didRadiantWin"] for r in rows)

    async def test_opendota_payloads_are_not_read(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """Mixing the two sources would put earned gold and net worth in the same column."""
        session.add(Match(match_id=7, start_time=datetime(2026, 8, 1, tzinfo=UTC)))
        await session.commit()
        await upsert_raw_matches(
            session,
            RawSource.OPENDOTA_MATCH,
            [{"match_id": 7, "version": 21, "radiant_win": True, "duration": 1800}],
        )
        await session.commit()

        report = await featurize(sessionmaker)

        assert report.matches_seen == 0
        assert report.snapshots == 0


class TestFilters:
    async def test_short_matches_are_skipped(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """Spec section 5.3: filtered by metadata, never by outcome."""
        await seed(session, [stub(99, durationSeconds=300)])

        report = await featurize(sessionmaker)

        assert report.matches_used == 0
        assert report.skipped == {"shorter than 12 minutes": 1}

    async def test_unparsed_matches_are_skipped(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        await seed(session, [stub(98, parsedDateTime=None)])

        report = await featurize(sessionmaker)

        assert report.skipped == {"not parsed": 1}

    async def test_matches_without_an_outcome_are_skipped(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        await seed(session, [stub(97, didRadiantWin=None)])

        report = await featurize(sessionmaker)

        assert report.skipped == {"no outcome to label with": 1}


class TestRebuild:
    async def test_rebuild_clears_rows_the_new_run_will_not_write(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """Rows are only ever upserted, so a change to the feature set or to the source
        leaves the old ones sitting next to the new. That is how one column ends up holding
        two different quantities.
        """
        async with sessionmaker() as seed_session:
            seed_session.add(Match(match_id=555, start_time=datetime(2026, 7, 1, tzinfo=UTC)))
            await seed_session.commit()
            seed_session.add(
                MatchSnapshot(
                    match_id=555,
                    minute=0,
                    # The shape a row built before the switch had: 30 features, gold_adv
                    # meaning earned gold.
                    features={"roshan_kills": 1.0},
                    radiant_win=True,
                )
            )
            await seed_session.commit()

        await seed(session, [stratz_match()])
        report = await featurize(sessionmaker, rebuild=True)

        assert report.deleted == 1
        async with sessionmaker() as check:
            stale = (
                (await check.execute(select(MatchSnapshot).where(MatchSnapshot.match_id == 555)))
                .scalars()
                .all()
            )
        assert stale == []
        assert report.snapshots > 0

    async def test_without_rebuild_existing_rows_survive(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        async with sessionmaker() as seed_session:
            seed_session.add(Match(match_id=555, start_time=datetime(2026, 7, 1, tzinfo=UTC)))
            await seed_session.commit()
            seed_session.add(MatchSnapshot(match_id=555, minute=0, features={}, radiant_win=True))
            await seed_session.commit()

        await seed(session, [stratz_match()])
        report = await featurize(sessionmaker)

        assert report.deleted == 0
        async with sessionmaker() as check:
            count = (
                (await check.execute(select(MatchSnapshot).where(MatchSnapshot.match_id == 555)))
                .scalars()
                .all()
            )
        assert len(count) == 1
