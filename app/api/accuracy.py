"""F6: the public accuracy dashboard, computed from predictions that have an outcome.

Not cosmetic. Every number on the rest of the site is a probability, and a probability
nobody has checked against reality is a decoration. This module is the check.

Two decisions here shape what the dashboard can honestly claim.

**Versions are never mixed.** `predictions` accumulates rows from every model that has ever
been served, including the baseline. Pooling them produces a calibration curve belonging to
no model at all - and the worse the retired model was, the more it drags the number that is
supposed to describe what is being served right now. Metrics are therefore always for one
`model_version`, and the page says which.

**One row per (match_id, minute).** The live poller writes a prediction every ~30 seconds,
so a minute usually holds two rows, and a paused game can hold dozens. Scoring all of them
weights the evaluation by how long each minute happened to last, which is a property of the
broadcast, not of the model. The earliest prediction in each minute wins: it is the one made
on the least information, and it is the one the viewer actually saw first.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.matches import Match
from app.db.models.training import Prediction
from app.ml.metrics import (
    accuracy,
    brier,
    by_bucket,
    expected_calibration_error,
    log_loss,
    reliability_curve,
)
from app.schemas.common import (
    MinuteBucketMetrics,
    ModelMetrics,
    ModelVersionInfo,
    ReliabilityBin,
)


@dataclass(frozen=True)
class ScoredPrediction:
    """A prediction whose match has since finished."""

    match_id: int
    minute: int
    p_radiant: float
    radiant_win: bool
    #: When it was served, not when the match ended. Drift is measured against the moment we
    #: made the claim - a model that went bad in July did so in July, whatever date the
    #: outcome arrived on.
    predicted_at: datetime


def _scored_rows(version: str | None = None) -> Select[Any]:
    """Predictions joined to their outcome, one row per (match, minute, version).

    `DISTINCT ON` keeps the first row of each group under the `ORDER BY`, which is why the
    ordering ends with `predicted_at`: the earliest prediction of the minute is the one kept.
    """
    statement = (
        select(
            Prediction.match_id,
            Prediction.minute,
            Prediction.p_radiant,
            Prediction.model_version,
            Prediction.predicted_at,
            Match.radiant_win,
        )
        .join(Match, Match.match_id == Prediction.match_id)
        .where(Match.radiant_win.is_not(None))
        .distinct(Prediction.match_id, Prediction.minute, Prediction.model_version)
        .order_by(
            Prediction.match_id,
            Prediction.minute,
            Prediction.model_version,
            Prediction.predicted_at,
        )
    )
    if version is not None:
        statement = statement.where(Prediction.model_version == version)
    return statement


async def load_scored(session: AsyncSession, version: str) -> list[ScoredPrediction]:
    rows = (await session.execute(_scored_rows(version))).all()
    return [
        ScoredPrediction(
            match_id=int(row.match_id),
            minute=int(row.minute),
            p_radiant=float(row.p_radiant),
            radiant_win=bool(row.radiant_win),
            predicted_at=row.predicted_at,
        )
        for row in rows
    ]


async def scored_versions(session: AsyncSession) -> list[ModelVersionInfo]:
    """Every model version that has predictions with a known outcome, newest activity first.

    A version with zero scored rows is absent rather than listed as empty: offering it in a
    picker only to show nothing is worse than not offering it.
    """
    inner = _scored_rows().subquery()
    rows = (
        await session.execute(
            select(inner.c.model_version, func.count().label("rows"))
            .group_by(inner.c.model_version)
            .order_by(func.count().desc())
        )
    ).all()
    return [
        ModelVersionInfo(version=str(row.model_version), sample_size=int(row.rows)) for row in rows
    ]


@dataclass(frozen=True)
class ServingProgress:
    """How much live evidence this version has accumulated, scored or not.

    The dashboard could only ever say how many matches it had *scored*, which made the
    smallness of that number look like a fault. Most of the answer is elsewhere: a version
    predicts only the matches that are on air while it serves, and scores them only once they
    end and their outcome is fetched. Both halves belong on the page.
    """

    predicted_matches: int
    first_prediction_at: datetime | None
    last_prediction_at: datetime | None


async def serving_progress(session: AsyncSession, version: str) -> ServingProgress:
    """Every match this version predicted, whether or not it can be scored yet.

    Counted in matches, not rows (invariant 3): the poller writes a row every thirty seconds,
    so an hour of one game would otherwise read as a hundred observations.
    """
    row = (
        await session.execute(
            select(
                func.count(func.distinct(Prediction.match_id)),
                func.min(Prediction.predicted_at),
                func.max(Prediction.predicted_at),
            ).where(Prediction.model_version == version)
        )
    ).one()
    return ServingProgress(
        predicted_matches=int(row[0] or 0),
        first_prediction_at=row[1],
        last_prediction_at=row[2],
    )


def metrics_from(version: str, scored: Sequence[ScoredPrediction]) -> ModelMetrics:
    """Assemble the dashboard payload. An empty slice yields empty tables, not zeroes.

    Zero log loss would read as a flawless model, which is exactly the wrong thing for a
    page whose entire job is to let a reader distrust us.
    """
    if not scored:
        return ModelMetrics(
            model_version=version,
            sample_size=0,
            matches=0,
            log_loss=None,
            brier=None,
            ece=None,
            by_minute=[],
            reliability=[],
        )

    minutes = [row.minute for row in scored]
    outcomes = [row.radiant_win for row in scored]
    probs = [row.p_radiant for row in scored]

    per_bucket = {
        name: by_bucket(minutes, outcomes, probs, metric)
        for name, metric in (("log_loss", log_loss), ("brier", brier), ("accuracy", accuracy))
    }
    # Counted through the same bucketing as the metrics rather than alongside it: a table
    # whose sample sizes disagree with its numbers is worse than one without sample sizes.
    counts = by_bucket(minutes, outcomes, probs, lambda labels, _probs: float(len(labels)))

    return ModelMetrics(
        model_version=version,
        sample_size=len(scored),
        matches=len({row.match_id for row in scored}),
        log_loss=log_loss(outcomes, probs),
        brier=brier(outcomes, probs),
        ece=expected_calibration_error(outcomes, probs),
        by_minute=[
            MinuteBucketMetrics(
                bucket=bucket,
                count=int(counts[bucket]),
                log_loss=per_bucket["log_loss"][bucket],
                brier=per_bucket["brier"][bucket],
                accuracy=per_bucket["accuracy"][bucket],
            )
            for bucket in counts
        ],
        reliability=[
            ReliabilityBin(predicted=point.predicted, observed=point.observed, count=point.count)
            for point in reliability_curve(outcomes, probs)
        ],
    )
