"""The scheduled half of phase 7: run the calibration check and say so when it fails.

`app.ml.drift` decides; this loads the evidence and writes the verdict where somebody will
see it. Kept apart because the deciding is worth testing without a database, and because a
monitor that can only be exercised against live data is a monitor nobody exercises.

Every version with scored predictions is checked, not only the one being served. A model
that was retired last week can still be the one a stakeholder is looking at on the accuracy
dashboard, and a version that drifted on its way out is worth knowing about before it comes
back.

Every version is checked in each segment separately - Tier 1, Pro, Excluded - so a shift in
which leagues were on air does not read as the model drifting.
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.accuracy import load_scored, scored_versions
from app.core.logging import get_logger
from app.db.session import get_session_factory
from app.domain.segments import SEGMENTS, Segment
from app.ml.drift import DriftVerdict, check_drift

log = get_logger(__name__)


async def drift_verdicts(
    session_factory: async_sessionmaker[AsyncSession], now: datetime
) -> list[tuple[Segment, DriftVerdict]]:
    """One verdict per (version, segment), versions in `scored_versions` order.

    Per segment because the feed's mix of leagues changes week to week: pooled, a week heavy
    with amateur cups would read as the model drifting. Tier 1 will mostly say "not enough
    data" - forty scored Tier 1 maps in a week happen only during a large event - and that is
    the honest verdict.
    """
    async with session_factory() as session:
        versions = await scored_versions(session)

    verdicts: list[tuple[Segment, DriftVerdict]] = []
    for info in versions:
        for segment in SEGMENTS:
            async with session_factory() as session:
                scored = await load_scored(session, info.version, segment)
            verdicts.append((segment, check_drift(info.version, scored, now)))
    return verdicts


async def check_calibration_drift(ctx: dict[str, Any]) -> int:
    """arq entry point. Returns the number of (version, segment) pairs found drifting.

    The alert is a log line at warning level, which is what this deployment can actually
    route somewhere. Anything louder - a page, an email - needs a destination the project
    does not have yet, and inventing one here would produce an alert with nowhere to go.
    """
    verdicts = await drift_verdicts(get_session_factory(), datetime.now(UTC))

    alerting = 0
    for segment, verdict in verdicts:
        fields = {"segment": segment, **verdict.as_log_fields()}
        if verdict.is_alerting:
            alerting += 1
            log.warning("calibration.drift", **fields)
        else:
            log.info("calibration.checked", **fields)

    if not verdicts:
        log.info("calibration.nothing_scored")
    return alerting
