"""Filling league and Valve tier on predictions logged before either was recorded.

Only NULLs are filled: `predictions` is the log of what was served (invariant 8), and a value
already there was written at serve time, which is better knowledge than anything now.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.raw import RawLiveSnapshot
from app.db.models.reference import League
from app.db.models.training import Prediction
from app.ingestion.segments_backfill import backfill_segments

AT = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


def prediction(match_id: int, **columns: object) -> Prediction:
    return Prediction(
        match_id=match_id,
        minute=10,
        predicted_at=AT,
        model_version="v1",
        p_radiant=0.5,
        features={},
        **columns,
    )


def snapshot(match_id: int, league_id: int, at: datetime) -> RawLiveSnapshot:
    return RawLiveSnapshot(
        match_id=match_id,
        source="live_league_games",
        captured_at=at,
        payload={"match_id": match_id, "league_id": league_id},
    )


async def rows(session: AsyncSession) -> dict[int, tuple[int | None, str | None]]:
    result = await session.execute(
        select(Prediction.match_id, Prediction.league_id, Prediction.valve_tier)
    )
    return {int(m): (league, tier) for m, league, tier in result.all()}


async def test_fills_league_from_the_latest_snapshot_and_tier_from_the_league(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker() as session:
        session.add(League(league_id=19696, valve_tier="professional"))
        session.add_all(
            [
                snapshot(1, 11111, AT - timedelta(minutes=5)),
                snapshot(1, 19696, AT),
                prediction(1),
            ]
        )
        await session.commit()

    report = await backfill_segments(sessionmaker)

    async with sessionmaker() as session:
        assert (await rows(session))[1] == (19696, "professional")
    assert (report.league_ids, report.valve_tiers) == (1, 1)


async def test_never_overwrites_what_was_recorded_at_serve_time(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker() as session:
        session.add(League(league_id=19696, valve_tier="professional"))
        session.add_all(
            [snapshot(1, 19696, AT), prediction(1, league_id=18445, valve_tier="excluded")]
        )
        await session.commit()

    report = await backfill_segments(sessionmaker)

    async with sessionmaker() as session:
        assert (await rows(session))[1] == (18445, "excluded")
    assert (report.league_ids, report.valve_tiers) == (0, 0)


async def test_a_league_without_a_valve_tier_leaves_the_prediction_unknown(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker() as session:
        session.add(League(league_id=19696))
        session.add_all([snapshot(1, 19696, AT), prediction(1)])
        await session.commit()

    await backfill_segments(sessionmaker)

    async with sessionmaker() as session:
        assert (await rows(session))[1] == (19696, None)


async def test_a_second_run_changes_nothing(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker() as session:
        session.add(League(league_id=19696, valve_tier="premium"))
        session.add_all([snapshot(1, 19696, AT), prediction(1)])
        await session.commit()

    await backfill_segments(sessionmaker)
    again = await backfill_segments(sessionmaker)

    assert (again.league_ids, again.valve_tiers) == (0, 0)


async def test_league_zero_in_a_snapshot_is_no_league(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker() as session:
        session.add_all([snapshot(1, 0, AT), prediction(1)])
        await session.commit()

    await backfill_segments(sessionmaker)

    async with sessionmaker() as session:
        assert (await rows(session))[1] == (None, None)
