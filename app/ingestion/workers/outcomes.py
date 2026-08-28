"""Closing the loop on predictions we have already served (spec sections 4.3, 8.1, 11).

Invariant 8 says every prediction is logged. That is only half a feedback loop: a logged
prediction is worth nothing until something says whether it was right. Until it does, the
accuracy dashboard (F6) has nothing to draw and the drift alert of phase 11 has nothing to
watch.

Waiting for the summary feed to sweep past those matches does not work, and the reason is
measured rather than assumed. On 2026-08-28 the top of OpenDota's `/proMatches` was exactly
the newest match we held and had not moved in ten hours, while 220 matches we had predicted
sat above it. A feed that arrives eventually still leaves the loop open for as long as it
takes, and nothing in the system notices the difference between "late" and "never".

So the question is asked the other way round. Instead of waiting for a bulk walk to reach
our matches, we name them: these are the ids we owe an answer for, fetch exactly those.
STRATZ is the source because it answers per id within minutes of a match ending - and
because its match payload is the same one phase 3 builds snapshots from, so resolving an
outcome also makes that map trainable instead of merely scored.
"""

from typing import Any

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.db.models.matches import Match
from app.db.models.raw import RawMatch
from app.db.models.training import Prediction
from app.db.session import get_session_factory
from app.ingestion.clients.stratz import StratzClient
from app.ingestion.sources import RawSource
from app.ingestion.workers.details import DetailsReport, MatchDetailSource, fetch_details

log = get_logger(__name__)


def _unresolved() -> Any:
    """Match ids we have predicted and cannot score yet.

    Two exclusions, and both matter. A match whose outcome is already known needs nothing.
    A match we have already fetched a payload for needs nothing either, even if the outcome
    is still missing - that gap belongs to `normalize`, and re-fetching would spend quota to
    re-learn what is already on disk.
    """
    outcome_known = exists().where(
        (Match.match_id == Prediction.match_id) & Match.radiant_win.is_not(None)
    )
    payload_held = exists().where(
        (RawMatch.match_id == Prediction.match_id)
        & (RawMatch.source == str(RawSource.STRATZ_MATCH))
    )
    return (
        select(Prediction.match_id)
        .where(~outcome_known, ~payload_held)
        .group_by(Prediction.match_id)
    )


async def select_unresolved_predictions(session: AsyncSession, limit: int) -> list[int]:
    """Newest first: a match that ended an hour ago is the one STRATZ can answer about, and
    the one whose absence from the dashboard is most visible."""
    statement = _unresolved().order_by(Prediction.match_id.desc()).limit(limit)
    return list((await session.execute(statement)).scalars().all())


async def count_unresolved_predictions(session: AsyncSession) -> int:
    return len(list((await session.execute(_unresolved())).scalars().all()))


async def resolve_outcomes(
    client: MatchDetailSource,
    session_factory: async_sessionmaker[AsyncSession],
    limit: int = 200,
) -> DetailsReport:
    """Fetch the matches behind unscored predictions. Stores payloads; `normalize` reads
    the outcome out of them."""
    async with session_factory() as session:
        match_ids = await select_unresolved_predictions(session, limit)

    report = await fetch_details(client, session_factory, match_ids, RawSource.STRATZ_MATCH)

    async with session_factory() as session:
        report.remaining = await count_unresolved_predictions(session)

    log.info("outcomes.done", **report.as_log_fields())
    return report


async def resolve_prediction_outcomes(ctx: dict[str, Any], limit: int = 200) -> int:
    """arq entry point. Returns payloads fetched."""
    async with StratzClient() as client:
        report = await resolve_outcomes(client, get_session_factory(), limit=limit)
    return report.fetched
