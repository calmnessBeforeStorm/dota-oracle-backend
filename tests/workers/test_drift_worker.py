"""The drift check runs per (version, segment).

Pooled, it would fire on a week where the feed happened to carry more amateur cups - a change
in which games were played, not in the model.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.matches import Match
from app.db.models.training import Prediction
from app.workers.drift import drift_verdicts

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)


async def test_every_version_is_checked_in_every_segment(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker() as session:
        session.add(Match(match_id=1, radiant_win=True))
        await session.flush()
        session.add(
            Prediction(
                match_id=1,
                minute=10,
                predicted_at=NOW - timedelta(days=1),
                model_version="v1",
                p_radiant=0.7,
                features={},
                league_id=None,
                valve_tier="excluded",
            )
        )
        await session.commit()

    verdicts = await drift_verdicts(sessionmaker, NOW)

    assert [(segment, verdict.model_version) for segment, verdict in verdicts] == [
        ("tier1", "v1"),
        ("pro", "v1"),
        ("excluded", "v1"),
    ]
    windows = {segment: verdict.recent for segment, verdict in verdicts}
    # Only the excluded slice holds the match; the other two have nothing to compare.
    assert windows["excluded"] is not None and windows["excluded"].matches == 1
    assert windows["tier1"] is None
    assert windows["pro"] is None


async def test_nothing_scored_yields_no_verdicts(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    assert await drift_verdicts(sessionmaker, NOW) == []
