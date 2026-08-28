"""The scheduled half of phase 7: run the calibration check and say so when it fails.

`app.ml.drift` decides; this loads the evidence and writes the verdict where somebody will
see it. Kept apart because the deciding is worth testing without a database, and because a
monitor that can only be exercised against live data is a monitor nobody exercises.

Every version with scored predictions is checked, not only the one being served. A model
that was retired last week can still be the one a stakeholder is looking at on the accuracy
dashboard, and a version that drifted on its way out is worth knowing about before it comes
back.
"""

from datetime import UTC, datetime
from typing import Any

from app.api.accuracy import load_scored, scored_versions
from app.core.logging import get_logger
from app.db.session import get_session_factory
from app.ml.drift import DriftVerdict, check_drift

log = get_logger(__name__)


async def check_calibration_drift(ctx: dict[str, Any]) -> int:
    """arq entry point. Returns the number of versions found to be drifting.

    The alert is a log line at warning level, which is what this deployment can actually
    route somewhere. Anything louder - a page, an email - needs a destination the project
    does not have yet, and inventing one here would produce an alert with nowhere to go.
    """
    session_factory = get_session_factory()
    now = datetime.now(UTC)

    async with session_factory() as session:
        versions = await scored_versions(session)

    alerting = 0
    for info in versions:
        async with session_factory() as session:
            scored = await load_scored(session, info.version)

        verdict: DriftVerdict = check_drift(info.version, scored, now)
        if verdict.is_alerting:
            alerting += 1
            log.warning("calibration.drift", **verdict.as_log_fields())
        else:
            log.info("calibration.checked", **verdict.as_log_fields())

    if not versions:
        log.info("calibration.nothing_scored")
    return alerting
