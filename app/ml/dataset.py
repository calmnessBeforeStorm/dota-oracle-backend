"""Loading and splitting `match_snapshots` for training (spec sections 5.1, 7.1).

Two rules decide everything in this module, and both are the kind that fail silently:

  - **Split by `match_id`, never by row.** Forty snapshots of one game are forty views of one
    outcome. A row-wise split puts some in train and some in test, the model recognises the
    game it has already seen, and the metrics come out excellent for a reason that will never
    reproduce in production.
  - **Split forward in time.** A random split lets the model learn from August to predict
    July. Section 7.1 forbids it outright.

Neither is enforced by the database, so it is enforced here and tested.

A note on the frozen holdout: section 7.1 asks for the last 2-3 months, untouched during
tuning. The current dataset spans 49 days in total, so that is arithmetically impossible.
The split below keeps the *shape* - a chronologically last slice that tuning never sees -
and takes it as a fraction instead. When the backfill goes deeper this should become a fixed
window again, and the fraction is a stopgap rather than a decision.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.db.models.matches import Match
from app.db.models.training import MatchSnapshot

log = get_logger(__name__)

#: Below this there is nothing to split into three slices that could carry a metric.
MIN_MATCHES = 10


@dataclass(frozen=True)
class SnapshotRow:
    match_id: int
    minute: int
    features: dict[str, float]
    radiant_win: bool
    start_time: datetime


@dataclass(frozen=True)
class Split:
    train: list[SnapshotRow]
    validation: list[SnapshotRow]
    holdout: list[SnapshotRow]

    def summary(self) -> dict[str, Any]:
        """What a human needs to sanity-check a run before trusting its numbers."""

        def window(rows: Sequence[SnapshotRow]) -> str:
            if not rows:
                return "-"
            first = min(r.start_time for r in rows).date()
            last = max(r.start_time for r in rows).date()
            return f"{first} .. {last}"

        return {
            "train_matches": len({r.match_id for r in self.train}),
            "validation_matches": len({r.match_id for r in self.validation}),
            "holdout_matches": len({r.match_id for r in self.holdout}),
            "train_rows": len(self.train),
            "validation_rows": len(self.validation),
            "holdout_rows": len(self.holdout),
            "train_window": window(self.train),
            "validation_window": window(self.validation),
            "holdout_window": window(self.holdout),
        }


async def load_snapshots(
    session_factory: async_sessionmaker[AsyncSession],
) -> list[SnapshotRow]:
    """Every snapshot, joined to its match for the timestamp the split needs.

    Returned in chronological order: the split depends on it, so producing it is this
    function's job rather than a precondition the caller has to remember.
    """
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(
                    MatchSnapshot.match_id,
                    MatchSnapshot.minute,
                    MatchSnapshot.features,
                    MatchSnapshot.radiant_win,
                    Match.start_time,
                )
                .join(Match, Match.match_id == MatchSnapshot.match_id)
                .order_by(Match.start_time, MatchSnapshot.match_id, MatchSnapshot.minute)
            )
        ).all()

    return [
        SnapshotRow(
            match_id=int(match_id),
            minute=int(minute),
            features={k: float(v) for k, v in dict(features).items()},
            radiant_win=bool(radiant_win),
            start_time=start_time,
        )
        for match_id, minute, features, radiant_win, start_time in rows
    ]


def split_by_time(
    rows: Sequence[SnapshotRow], validation: float = 0.1, holdout: float = 0.2
) -> Split:
    """Chronological three-way split, grouped by match.

    `validation` is where the calibrator is fitted and where any tuning may look.
    `holdout` is the frozen slice: it is scored once, at the end, and never tuned against.
    """
    if not 0 < validation < 1 or not 0 < holdout < 1 or validation + holdout >= 1:
        raise ValueError("validation and holdout must be fractions leaving a non-empty train")

    ordered_matches: list[int] = []
    seen: set[int] = set()
    for row in sorted(rows, key=lambda r: (r.start_time, r.match_id)):
        if row.match_id not in seen:
            seen.add(row.match_id)
            ordered_matches.append(row.match_id)

    total = len(ordered_matches)
    if total < MIN_MATCHES:
        raise ValueError(f"too few matches to split: {total}, need at least {MIN_MATCHES}")

    n_holdout = max(1, round(total * holdout))
    n_validation = max(1, round(total * validation))
    n_train = total - n_holdout - n_validation
    if n_train < 1:
        raise ValueError(f"too few matches to split: {total} leaves no training slice")

    train_ids = set(ordered_matches[:n_train])
    validation_ids = set(ordered_matches[n_train : n_train + n_validation])

    split = Split(
        train=[r for r in rows if r.match_id in train_ids],
        validation=[r for r in rows if r.match_id in validation_ids],
        holdout=[
            r for r in rows if r.match_id not in train_ids and r.match_id not in validation_ids
        ],
    )
    log.info("dataset.split", **split.summary())
    return split


#: How many matches the pure-Python baselines are fitted on.
#:
#: They are one-, two- and three-parameter models fitted by gradient descent in plain Python,
#: which costs O(iterations x rows). Measured: at 101605 training rows the gate took about
#: twenty times longer than the LightGBM fit it exists to judge - and the training set is
#: still growing by a factor of six as the backfill lands. Three coefficients do not need a
#: hundred thousand correlated rows.
#:
#: The cap is on matches, never on rows: snapshots of one game move together (section 5.1),
#: so sampling rows would keep the row count while quietly narrowing the variety of games
#: behind the fit.
BASELINE_FIT_MATCHES = 600


def baseline_fit_slice(
    rows: Sequence[SnapshotRow], matches: int = BASELINE_FIT_MATCHES
) -> list[SnapshotRow]:
    """A cheaper stand-in for the training slice, for fitting the baselines only.

    Spread evenly across the window rather than taken from the front. The training slice is
    chronological, so a prefix would fit every baseline on the oldest patch in the data and
    then judge the model on the newest.
    """
    if matches < 2:
        raise ValueError("a fit needs at least two matches")

    order: dict[int, None] = {}
    for row in rows:
        order.setdefault(row.match_id, None)
    if len(order) <= matches:
        return list(rows)

    ids = list(order)
    # Spans both ends of the window. A plain `index * len(ids) / matches` stops short of the
    # newest matches - with a thousand of them and a cap of ten it never reaches past the
    # nine-hundredth, and the baselines end up fitted on a slightly older game than the model.
    last = len(ids) - 1
    keep = {ids[round(index * last / (matches - 1))] for index in range(matches)}
    return [row for row in rows if row.match_id in keep]
