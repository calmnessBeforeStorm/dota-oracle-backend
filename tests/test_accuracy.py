"""F6: scoring served predictions against outcomes (spec sections 7.2, 8.1).

The dashboard exists so a visitor can decide whether to believe the rest of the site, which
makes every way it could flatter the model a bug: pooling a retired version into the current
one, weighting a paused minute by how long it was paused, or reporting an empty slice as a
perfect score.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.accuracy import ScoredPrediction, load_scored, metrics_from, scored_versions
from app.db.models.matches import Match
from app.db.models.training import Prediction

BASE = datetime(2026, 8, 1, tzinfo=UTC)
VERSION = "live-v1"


async def add_match(session: AsyncSession, match_id: int, radiant_win: bool | None) -> None:
    session.add(Match(match_id=match_id, radiant_win=radiant_win, start_time=BASE))
    await session.flush()


async def add_prediction(
    session: AsyncSession,
    match_id: int,
    minute: int,
    p_radiant: float,
    *,
    version: str = VERSION,
    at: datetime | None = None,
) -> None:
    session.add(
        Prediction(
            match_id=match_id,
            minute=minute,
            predicted_at=at or BASE + timedelta(minutes=minute),
            model_version=version,
            p_radiant=p_radiant,
            features={},
        )
    )
    await session.flush()


def scored(*rows: tuple[int, int, float, bool]) -> list[ScoredPrediction]:
    return [
        ScoredPrediction(
            match_id=m,
            minute=minute,
            p_radiant=p,
            radiant_win=win,
            # Irrelevant to these metrics; the drift check is what reads it.
            predicted_at=BASE + timedelta(minutes=minute),
        )
        for m, minute, p, win in rows
    ]


class TestLoading:
    async def test_a_match_without_a_result_is_not_scored(self, session: AsyncSession) -> None:
        """A live match has no outcome to be right or wrong about. Scoring it against `false`
        - the shape a naive join produces - would count every ongoing match as a Dire win."""
        await add_match(session, 1, radiant_win=None)
        await add_prediction(session, 1, minute=10, p_radiant=0.8)

        assert await load_scored(session, VERSION) == []

    async def test_a_prediction_for_an_unknown_match_is_not_scored(
        self, session: AsyncSession
    ) -> None:
        """The poller logs predictions for live matches; the row in `matches` only appears
        once the backfill catches up. Until then there is nothing to compare against."""
        await add_prediction(session, 999, minute=10, p_radiant=0.8)

        assert await load_scored(session, VERSION) == []

    async def test_only_the_requested_version_is_returned(self, session: AsyncSession) -> None:
        await add_match(session, 1, radiant_win=True)
        await add_prediction(session, 1, minute=10, p_radiant=0.9, version="live-v1")
        await add_prediction(session, 1, minute=10, p_radiant=0.1, version="baseline")

        rows = await load_scored(session, "live-v1")

        assert [row.p_radiant for row in rows] == [0.9]

    async def test_a_minute_is_scored_once_however_often_it_was_polled(
        self, session: AsyncSession
    ) -> None:
        """The poller writes every ~30 seconds and a paused game holds a minute for ages.
        Counting each row would weight the evaluation by broadcast timing, not by model
        behaviour: one stalled minute would outvote a whole match."""
        await add_match(session, 1, radiant_win=True)
        for offset in range(6):
            await add_prediction(
                session,
                1,
                minute=10,
                p_radiant=0.5 + offset / 100,
                at=BASE + timedelta(seconds=30 * offset),
            )

        rows = await load_scored(session, VERSION)

        assert len(rows) == 1
        # The earliest of the minute: made on the least information, and seen first.
        assert rows[0].p_radiant == pytest.approx(0.5)

    async def test_different_minutes_of_one_match_are_all_kept(self, session: AsyncSession) -> None:
        await add_match(session, 1, radiant_win=True)
        for minute in (5, 6, 7):
            await add_prediction(session, 1, minute=minute, p_radiant=0.6)

        assert len(await load_scored(session, VERSION)) == 3


class TestVersions:
    async def test_versions_are_listed_with_their_sample_sizes(self, session: AsyncSession) -> None:
        await add_match(session, 1, radiant_win=True)
        await add_prediction(session, 1, minute=10, p_radiant=0.9, version="live-v1")
        await add_prediction(session, 1, minute=11, p_radiant=0.9, version="live-v1")
        await add_prediction(session, 1, minute=10, p_radiant=0.5, version="baseline")

        listed = {info.version: info.sample_size for info in await scored_versions(session)}

        assert listed == {"live-v1": 2, "baseline": 1}

    async def test_a_version_with_nothing_scored_is_absent(self, session: AsyncSession) -> None:
        """Offering a version in a picker and then showing nothing is worse than not
        offering it."""
        await add_match(session, 1, radiant_win=None)
        await add_prediction(session, 1, minute=10, p_radiant=0.9, version="live-v2")

        assert await scored_versions(session) == []


class TestMetrics:
    def test_an_empty_slice_reports_nothing_rather_than_zero(self) -> None:
        """Zero log loss is the score of a flawless model. On a page whose purpose is to let
        a reader distrust us, that is the worst possible way to say "no data yet"."""
        metrics = metrics_from(VERSION, [])

        assert metrics.sample_size == 0
        assert metrics.log_loss is None
        assert metrics.brier is None
        assert metrics.ece is None
        assert metrics.by_minute == []

    def test_metrics_are_broken_out_per_minute_bucket(self) -> None:
        metrics = metrics_from(
            VERSION,
            scored((1, 2, 0.5, True), (1, 12, 0.9, True), (1, 35, 0.99, True)),
        )

        assert [row.bucket for row in metrics.by_minute] == ["0-4", "10-14", "30+"]
        assert [row.count for row in metrics.by_minute] == [1, 1, 1]

    def test_bucket_order_follows_the_clock_not_the_data(self) -> None:
        """Rows arrive in whatever order the join produced. A table that jumps from 30+ to
        5-9 hides the one trend it exists to show: the model improving as the game resolves."""
        metrics = metrics_from(
            VERSION, scored((1, 40, 0.9, True), (1, 3, 0.5, True), (1, 22, 0.7, True))
        )

        assert [row.bucket for row in metrics.by_minute] == ["0-4", "20-24", "30+"]

    def test_a_confident_correct_call_beats_a_hedge(self) -> None:
        confident = metrics_from(VERSION, scored((1, 10, 0.95, True)))
        hedging = metrics_from(VERSION, scored((1, 10, 0.55, True)))

        assert confident.log_loss is not None and hedging.log_loss is not None
        assert confident.log_loss < hedging.log_loss

    def test_matches_are_counted_distinctly_from_rows(self) -> None:
        """Sample size in rows flatters a dashboard built on three matches: forty snapshots
        of one game are forty correlated rows, not forty independent observations."""
        metrics = metrics_from(
            VERSION, scored((1, 10, 0.6, True), (1, 11, 0.6, True), (2, 10, 0.6, False))
        )

        assert metrics.sample_size == 3
        assert metrics.matches == 2

    def test_the_reliability_curve_reports_what_actually_happened(self) -> None:
        """Four predictions of ~90% where the favourite won three times: the curve must say
        75%, because that gap is the whole point of the page."""
        rows = scored(
            (1, 10, 0.9, True), (2, 10, 0.9, True), (3, 10, 0.9, True), (4, 10, 0.9, False)
        )

        curve = metrics_from(VERSION, rows).reliability

        assert len(curve) == 1
        assert curve[0].predicted == pytest.approx(0.9)
        assert curve[0].observed == pytest.approx(0.75)
        assert curve[0].count == 4

    def test_ece_measures_the_gap_between_promise_and_outcome(self) -> None:
        honest = metrics_from(VERSION, scored((1, 10, 0.5, True), (2, 10, 0.5, False)))
        overconfident = metrics_from(VERSION, scored((1, 10, 0.99, True), (2, 10, 0.99, False)))

        assert honest.ece is not None and overconfident.ece is not None
        assert honest.ece < overconfident.ece


class TestTrainingStatus:
    """The page kept being read as "this model was never validated".

    It said 9 matches and stopped, because 9 was all it knew: predictions exist only for
    matches that were on air while a version served. The holdout - 1293 matches, scored once,
    offline - lives on the model card and was not exposed at all.
    """

    @pytest.mark.asyncio
    async def test_progress_counts_every_predicted_match_not_only_scored_ones(
        self, session: AsyncSession
    ) -> None:
        from app.api.accuracy import serving_progress

        session.add_all([Match(match_id=1, radiant_win=True), Match(match_id=2)])
        await session.flush()
        await add_prediction(session, 1, 5, 0.6, version="v1")
        await add_prediction(session, 1, 6, 0.6, version="v1")
        await add_prediction(session, 2, 5, 0.4, version="v1")

        progress = await serving_progress(session, "v1")

        # Two matches, three rows. Counted in matches (invariant 3).
        assert progress.predicted_matches == 2
        assert progress.first_prediction_at is not None
        assert progress.last_prediction_at is not None

    @pytest.mark.asyncio
    async def test_a_version_that_never_served_has_no_progress(self, session: AsyncSession) -> None:
        from app.api.accuracy import serving_progress

        progress = await serving_progress(session, "never-served")

        assert progress.predicted_matches == 0
        assert progress.first_prediction_at is None

    def test_a_baseline_has_no_card_and_says_so_rather_than_zero(self) -> None:
        """`holdout_matches: 0` on a baseline would read as a model that failed validation.
        A baseline is code; nobody held a slice back from it."""
        from app.api.routes.model import _training

        assert _training("baseline-logistic-0.2") is None
