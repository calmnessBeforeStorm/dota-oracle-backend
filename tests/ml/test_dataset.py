"""Loading and splitting the training set (spec sections 5.1, 7.1).

Two rules decide everything here and both are easy to break without noticing:

  - **Split by `match_id`, never by row.** Forty snapshots of one game are forty views of the
    same outcome; a row-wise split puts some of them in train and some in test, and the model
    scores brilliantly on a game it has already seen.
  - **Split forward in time.** A random split lets the model learn from August to predict
    July. Metrics come out flattering and production disappoints.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.matches import Match
from app.db.models.training import MatchSnapshot
from app.ml.dataset import (
    SnapshotRow,
    Split,
    baseline_fit_slice,
    load_snapshots,
    split_by_time,
)


def row(match_id: int, minute: int) -> SnapshotRow:
    return SnapshotRow(
        match_id=match_id,
        minute=minute,
        features={"gold_adv": 0.0, "minute": float(minute)},
        radiant_win=True,
        start_time=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=match_id),
    )


async def seed_matches(session: AsyncSession, count: int, snapshots_per_match: int = 5) -> None:
    """`count` matches one day apart, oldest first, each with a handful of snapshots."""
    base = datetime(2026, 1, 1, tzinfo=UTC)
    for index in range(count):
        match_id = 1000 + index
        session.add(Match(match_id=match_id, start_time=base + timedelta(days=index)))
    await session.commit()

    for index in range(count):
        match_id = 1000 + index
        for minute in range(snapshots_per_match):
            session.add(
                MatchSnapshot(
                    match_id=match_id,
                    minute=minute,
                    features={"gold_adv": float(index * 100), "minute": float(minute)},
                    radiant_win=index % 2 == 0,
                )
            )
    await session.commit()


class TestLoading:
    async def test_rows_carry_features_label_minute_and_time(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        await seed_matches(session, 3)

        rows = await load_snapshots(sessionmaker)

        assert len(rows) == 15
        first = rows[0]
        assert first.match_id == 1000
        assert first.minute == 0
        assert first.radiant_win is True
        assert first.features["gold_adv"] == 0.0
        assert first.start_time == datetime(2026, 1, 1, tzinfo=UTC)

    async def test_rows_come_back_in_time_order(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """The split relies on this ordering, so it is the loader's job, not the caller's."""
        await seed_matches(session, 5)

        rows = await load_snapshots(sessionmaker)

        times = [row.start_time for row in rows]
        assert times == sorted(times)

    async def test_an_empty_table_loads_to_nothing(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        assert await load_snapshots(sessionmaker) == []


class TestSplitting:
    def rows(self, count: int) -> list:
        base = datetime(2026, 1, 1, tzinfo=UTC)
        from app.ml.dataset import SnapshotRow

        return [
            SnapshotRow(
                match_id=1000 + index,
                minute=minute,
                features={"gold_adv": 0.0, "minute": float(minute)},
                radiant_win=index % 2 == 0,
                start_time=base + timedelta(days=index),
            )
            for index in range(count)
            for minute in range(5)
        ]

    def test_no_match_appears_in_two_slices(self) -> None:
        """The rule the whole module exists for (spec section 5.1)."""
        split = split_by_time(self.rows(100))

        train = {row.match_id for row in split.train}
        val = {row.match_id for row in split.validation}
        holdout = {row.match_id for row in split.holdout}

        assert train & val == set()
        assert train & holdout == set()
        assert val & holdout == set()

    def test_every_row_lands_somewhere(self) -> None:
        rows = self.rows(100)
        split = split_by_time(rows)
        assert len(split.train) + len(split.validation) + len(split.holdout) == len(rows)

    def test_slices_are_ordered_in_time(self) -> None:
        """Train is the past, holdout is the future. The reverse is a time machine."""
        split = split_by_time(self.rows(100))

        assert max(r.start_time for r in split.train) <= min(r.start_time for r in split.validation)
        assert max(r.start_time for r in split.validation) <= min(
            r.start_time for r in split.holdout
        )

    def test_proportions_are_by_match_not_by_row(self) -> None:
        split = split_by_time(self.rows(100), validation=0.1, holdout=0.2)

        assert len({r.match_id for r in split.train}) == 70
        assert len({r.match_id for r in split.validation}) == 10
        assert len({r.match_id for r in split.holdout}) == 20

    def test_a_sample_too_small_to_split_is_refused(self) -> None:
        """Silently returning empty slices would show up much later as a metric on no rows."""
        with pytest.raises(ValueError, match="too few matches"):
            split_by_time(self.rows(3))

    def test_summary_reports_what_a_human_needs_to_sanity_check(self) -> None:
        split: Split = split_by_time(self.rows(100))
        summary = split.summary()

        assert summary["train_matches"] == 70
        assert summary["holdout_matches"] == 20
        assert summary["train_rows"] == 350
        assert "train_window" in summary and "holdout_window" in summary


class TestBaselineFitSlice:
    """Bounding what the pure-Python baselines are fitted on (spec sections 5.1, 7.3).

    Measured: at 101605 training rows the evaluation gate took roughly twenty times longer
    than the LightGBM fit it exists to judge, because three coefficients were being found by
    gradient descent over a hundred thousand correlated rows.
    """

    def test_a_small_slice_is_returned_whole(self) -> None:
        rows = [row(match_id=m, minute=0) for m in range(10)]

        assert len(baseline_fit_slice(rows, matches=600)) == 10

    def test_a_large_slice_is_capped_by_match(self) -> None:
        rows = [row(match_id=m, minute=minute) for m in range(1000) for minute in range(5)]

        kept = baseline_fit_slice(rows, matches=100)

        assert len({r.match_id for r in kept}) == 100

    def test_every_row_of_a_kept_match_survives(self) -> None:
        """Sampling rows would keep the row count while narrowing the variety of games behind
        the fit - snapshots of one game move together (section 5.1)."""
        rows = [row(match_id=m, minute=minute) for m in range(1000) for minute in range(5)]

        kept = baseline_fit_slice(rows, matches=100)

        assert len(kept) == 500
        for match_id in {r.match_id for r in kept}:
            assert sum(1 for r in kept if r.match_id == match_id) == 5

    def test_the_sample_spans_the_whole_window(self) -> None:
        """A prefix would fit every baseline on the oldest patch in the data and then judge
        the model on the newest."""
        rows = [row(match_id=m, minute=0) for m in range(1000)]

        kept = {r.match_id for r in baseline_fit_slice(rows, matches=10)}

        assert min(kept) < 100
        assert max(kept) > 900
