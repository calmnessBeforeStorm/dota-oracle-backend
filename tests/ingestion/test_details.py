"""Match detail fetching and parsing (spec section 2.2/A2, phase 1)."""

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.matches import Match, MatchDraft, MatchObjective, MatchPlayer
from app.ingestion.normalize import (
    normalize_match_details,
    normalize_pro_matches,
    parse_match_detail,
)
from app.ingestion.repository import count_raw_matches, match_id_of, upsert_raw_matches
from app.ingestion.sources import RawSource
from app.ingestion.workers.details import (
    count_missing_details,
    run_details_backfill,
    select_matches_missing_details,
)
from tests.ingestion.test_normalize import summary


def detail(match_id: int, *, version: int | None = 22, patch: int = 60) -> dict[str, Any]:
    """Shaped after a real /matches/{id} response, trimmed to what the parser reads."""
    return {
        "match_id": match_id,
        "radiant_win": False,
        "duration": 2971,
        "patch": patch,
        "version": version,
        # Both come back null from this endpoint - verified against the live API.
        "series_id": None,
        "series_type": None,
        "players": [
            {
                "player_slot": slot,
                "account_id": 1000 + slot,
                "hero_id": 10 + slot,
                "lane_role": 1,
                "kills": 5,
                "deaths": 2,
                "assists": 3,
                "last_hits": 200,
                "denies": 10,
                "net_worth": 20000,
                "gold_per_min": 500,
                "xp_per_min": 600,
                "leaver_status": 0,
            }
            for slot in [0, 1, 2, 3, 4, 128, 129, 130, 131, 132]
        ],
        "picks_bans": [
            {"is_pick": order >= 8, "hero_id": 80 + order, "team": order % 2, "order": order}
            for order in range(24)
        ],
        "objectives": [
            # `key` is a string here and a number two rows down: the payload is not uniform.
            {"time": -46, "type": "CHAT_MESSAGE_FIRSTBLOOD", "key": "9", "player_slot": 0},
            {"time": 39, "type": "CHAT_MESSAGE_COURIER_LOST", "team": 3, "value": 25},
            {"time": 600, "type": "building_kill", "key": 12, "player_slot": 129},
        ],
    }


class FakeOpenDota:
    def __init__(self, fail_on: set[int] | None = None) -> None:
        self.fail_on = fail_on or set()
        self.calls: list[int] = []

    async def match(self, match_id: int) -> dict[str, Any]:
        self.calls.append(match_id)
        if match_id in self.fail_on:
            raise RuntimeError("upstream exploded")
        return detail(match_id)


class FakeStratz:
    """Shaped after a real STRATZ `match(id:)` response, trimmed to what is stored."""

    def __init__(self) -> None:
        self.calls: list[int] = []

    async def match(self, match_id: int) -> dict[str, Any]:
        self.calls.append(match_id)
        return {
            "id": match_id,
            "didRadiantWin": True,
            "durationSeconds": 1800,
            "parsedDateTime": 1787702910,
            "players": [],
            "towerDeaths": [],
        }


class TestParsing:
    def test_splits_players_by_slot(self) -> None:
        """Slots 0-4 are radiant, 128+ dire. It is the only side marker in the payload."""
        parsed = parse_match_detail(detail(1))
        players = parsed["players"]
        assert len(players) == 10
        assert sum(p["is_radiant"] for p in players) == 5
        assert all(p["is_radiant"] for p in players if p["player_slot"] < 128)

    def test_keeps_full_draft_with_order(self) -> None:
        drafts = parse_match_detail(detail(1))["drafts"]
        assert len(drafts) == 24
        assert [d["order"] for d in drafts] == list(range(24))
        assert sum(d["is_pick"] for d in drafts) == 16

    def test_objectives_get_positional_keys(self) -> None:
        objectives = parse_match_detail(detail(1))["objectives"]
        assert [o["ordinal"] for o in objectives] == [0, 1, 2]

    def test_objective_key_is_stringified(self) -> None:
        """It arrives as a string for some event types and a number for others."""
        objectives = parse_match_detail(detail(1))["objectives"]
        assert objectives[0]["key"] == "9"
        assert objectives[2]["key"] == "12"

    def test_negative_objective_time_survives(self) -> None:
        """Pre-horn first blood is a real event, not bad data."""
        assert parse_match_detail(detail(1))["objectives"][0]["time"] == -46


class TestDetailNormalization:
    async def test_loads_rosters_draft_and_objectives(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        await upsert_raw_matches(session, RawSource.OPENDOTA_PRO_MATCHES, [summary(1)])
        await session.commit()
        await normalize_pro_matches(sessionmaker)

        await upsert_raw_matches(session, RawSource.OPENDOTA_MATCH, [detail(1)])
        await session.commit()
        report = await normalize_match_details(sessionmaker)

        assert (report.match_players, report.match_drafts, report.match_objectives) == (10, 24, 3)
        assert len((await session.execute(select(MatchPlayer))).scalars().all()) == 10
        assert len((await session.execute(select(MatchDraft))).scalars().all()) == 24
        assert len((await session.execute(select(MatchObjective))).scalars().all()) == 3

    async def test_rerun_does_not_duplicate(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """The normalized layer is rebuilt from raw whenever parsing changes. Objectives are
        the risky table: with a surrogate id they would append the whole log every time."""
        await upsert_raw_matches(session, RawSource.OPENDOTA_PRO_MATCHES, [summary(1)])
        await upsert_raw_matches(session, RawSource.OPENDOTA_MATCH, [detail(1)])
        await session.commit()
        await normalize_pro_matches(sessionmaker)

        await normalize_match_details(sessionmaker)
        await normalize_match_details(sessionmaker)
        await session.commit()

        assert len((await session.execute(select(MatchObjective))).scalars().all()) == 3
        assert len((await session.execute(select(MatchPlayer))).scalars().all()) == 10

    async def test_enriches_match_without_touching_series(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """series_id and series_type are null in the detail endpoint, so series membership
        established from the summaries must survive a detail pass."""
        await upsert_raw_matches(
            session, RawSource.OPENDOTA_PRO_MATCHES, [summary(1, series_id=42)]
        )
        await session.commit()
        await normalize_pro_matches(sessionmaker)

        before = (await session.execute(select(Match))).scalar_one()
        series_before = before.series_id
        assert series_before is not None

        await upsert_raw_matches(session, RawSource.OPENDOTA_MATCH, [detail(1, patch=60)])
        await session.commit()
        await normalize_match_details(sessionmaker)

        await session.refresh(before)
        assert before.series_id == series_before
        assert before.patch == 60

    async def test_unparsed_detail_marks_match(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        await upsert_raw_matches(session, RawSource.OPENDOTA_PRO_MATCHES, [summary(1)])
        await upsert_raw_matches(session, RawSource.OPENDOTA_MATCH, [detail(1, version=None)])
        await session.commit()
        await normalize_pro_matches(sessionmaker)
        await normalize_match_details(sessionmaker)

        await session.commit()
        match = (await session.execute(select(Match))).scalar_one()
        assert match.is_parsed is False


class TestDetailsBackfill:
    async def _seed(self, session: AsyncSession, sessionmaker: Any, count: int) -> None:
        await upsert_raw_matches(
            session,
            RawSource.OPENDOTA_PRO_MATCHES,
            [summary(i, offset_seconds=i * 60) for i in range(1, count + 1)],
        )
        await session.commit()
        await normalize_pro_matches(sessionmaker)

    async def test_fetches_only_what_is_missing(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        await self._seed(session, sessionmaker, 3)

        client = FakeOpenDota()
        first = await run_details_backfill(client, sessionmaker, limit=2)
        assert first.fetched == 2
        assert first.remaining == 1

        second = await run_details_backfill(client, sessionmaker, limit=10)
        assert second.requested == 1  # the two already stored are not re-requested
        assert second.remaining == 0

    async def test_newest_first_by_default(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """A run stopped halfway should have covered the most recent history."""
        await self._seed(session, sessionmaker, 3)

        client = FakeOpenDota()
        await run_details_backfill(client, sessionmaker, limit=1)
        assert client.calls == [3]

        older = FakeOpenDota()
        await run_details_backfill(older, sessionmaker, limit=1, newest_first=False)
        assert older.calls == [1]

    async def test_one_bad_match_does_not_end_the_run(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """The run lasts hours; a single upstream failure must not cost the whole thing."""
        await self._seed(session, sessionmaker, 3)

        client = FakeOpenDota(fail_on={2})
        report = await run_details_backfill(client, sessionmaker, limit=3)

        assert report.fetched == 2
        assert report.failed == 1
        assert len(client.calls) == 3

    async def test_counts_missing(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        await self._seed(session, sessionmaker, 4)
        assert await count_missing_details(session) == 4

        await run_details_backfill(FakeOpenDota(), sessionmaker, limit=4)
        await session.commit()
        assert await count_missing_details(session) == 0


@pytest.mark.parametrize("bad", [{"players": None}, {"picks_bans": None}, {"objectives": None}])
def test_parser_tolerates_missing_sections(bad: dict[str, Any]) -> None:
    """Unparsed and abandoned matches come back with sections missing entirely."""
    payload = detail(1) | bad
    parsed = parse_match_detail(payload)
    assert isinstance(parsed["players"], list)
    assert isinstance(parsed["drafts"], list)
    assert isinstance(parsed["objectives"], list)


class TestRateLimiting:
    """A 429 means the quota is gone, and the wrong response to it is expensive.

    Observed on a real run: OpenDota cut us off after 685 matches and the loop worked
    through the remaining 4500 at one failure per second, fetching nothing and earning an
    IP ban for the trouble.
    """

    class RateLimitedClient:
        def __init__(self, succeed_first: int = 0) -> None:
            self.succeed_first = succeed_first
            self.calls = 0

        async def match(self, match_id: int) -> dict[str, Any]:
            from app.ingestion.clients.base import RateLimitedError

            self.calls += 1
            if self.calls > self.succeed_first:
                raise RateLimitedError(retry_after=None)
            return detail(match_id)

    async def test_the_run_stops_instead_of_burning_the_list(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        await upsert_raw_matches(
            session,
            RawSource.OPENDOTA_PRO_MATCHES,
            [summary(i, offset_seconds=i * 60) for i in range(1, 21)],
        )
        await session.commit()
        await normalize_pro_matches(sessionmaker)

        client = self.RateLimitedClient(succeed_first=3)
        report = await run_details_backfill(client, sessionmaker, limit=20)

        assert report.fetched == 3
        # Stopped on the fourth rather than trying the remaining sixteen.
        assert client.calls == 4
        assert "rate limited" in report.stopped_because

    async def test_what_was_fetched_before_the_limit_is_kept(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """Resuming later must not re-pay for matches already stored."""
        await upsert_raw_matches(
            session,
            RawSource.OPENDOTA_PRO_MATCHES,
            [summary(i, offset_seconds=i * 60) for i in range(1, 11)],
        )
        await session.commit()
        await normalize_pro_matches(sessionmaker)

        await run_details_backfill(self.RateLimitedClient(succeed_first=2), sessionmaker, limit=10)
        await session.commit()
        assert await count_missing_details(session) == 8

    async def test_an_upstream_failing_everything_gives_up(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        await upsert_raw_matches(
            session,
            RawSource.OPENDOTA_PRO_MATCHES,
            [summary(i, offset_seconds=i * 60) for i in range(1, 41)],
        )
        await session.commit()
        await normalize_pro_matches(sessionmaker)

        client = FakeOpenDota(fail_on=set(range(1, 41)))
        report = await run_details_backfill(client, sessionmaker, limit=40)

        assert report.fetched == 0
        assert len(client.calls) == 20  # the give-up limit, not all forty
        assert "20 times in a row" in report.stopped_because

    async def test_it_gives_up_even_after_a_good_start(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """The check used to also require that nothing had been fetched, so a run that
        started fine and hit a wall later never stopped. Observed on the real STRATZ
        backfill: it fetched 2000-odd maps, ran into the hourly allowance, and then worked
        through the remaining 1600 ids at one rejected request every two seconds - logging a
        failure each time and exiting 0. That is how an IP ban is earned by a process that
        looks like it is still working."""
        await upsert_raw_matches(
            session,
            RawSource.OPENDOTA_PRO_MATCHES,
            [summary(i, offset_seconds=i * 60) for i in range(1, 61)],
        )
        await session.commit()
        await normalize_pro_matches(sessionmaker)

        # Newest first, so ids 60..56 succeed and everything older fails.
        client = FakeOpenDota(fail_on=set(range(1, 56)))
        report = await run_details_backfill(client, sessionmaker, limit=60)

        assert report.fetched == 5
        assert len(client.calls) == 25  # five good, then twenty in a row
        assert "20 times in a row" in report.stopped_because

    async def test_a_scattered_failure_does_not_stop_the_run(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """The counter resets on success. A handful of unparseable maps spread through the
        history is normal and must not end a backfill that is otherwise working."""
        await upsert_raw_matches(
            session,
            RawSource.OPENDOTA_PRO_MATCHES,
            [summary(i, offset_seconds=i * 60) for i in range(1, 41)],
        )
        await session.commit()
        await normalize_pro_matches(sessionmaker)

        client = FakeOpenDota(fail_on={5, 10, 15, 20, 25, 30})
        report = await run_details_backfill(client, sessionmaker, limit=40)

        assert report.failed == 6
        assert report.fetched == 34
        assert report.stopped_because == "finished the list"


class TestPerSourceBookkeeping:
    """A map fetched from OpenDota is still missing from STRATZ, and the other way round.
    One shared counter would report the backfill as finished with half of it unrun.
    """

    async def _seed(self, session: AsyncSession, sessionmaker: Any, count: int) -> None:
        await upsert_raw_matches(
            session,
            RawSource.OPENDOTA_PRO_MATCHES,
            [summary(i, offset_seconds=i * 60) for i in range(1, count + 1)],
        )
        await session.commit()
        await normalize_pro_matches(sessionmaker)

    async def test_missing_details_are_counted_per_source(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        await self._seed(session, sessionmaker, 3)

        await run_details_backfill(
            FakeOpenDota(), sessionmaker, limit=2, source=RawSource.OPENDOTA_MATCH
        )

        async with sessionmaker() as check:
            assert await count_missing_details(check, RawSource.OPENDOTA_MATCH) == 1
            assert await count_missing_details(check, RawSource.STRATZ_MATCH) == 3
            assert len(await select_matches_missing_details(check, 10)) == 1
            assert (
                len(await select_matches_missing_details(check, 10, source=RawSource.STRATZ_MATCH))
                == 3
            )

    async def test_backfill_writes_under_the_requested_source(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """The fake speaks STRATZ, not OpenDota. That matters: STRATZ calls the map's id
        `id`, and a raw-layer writer that only knows `match_id` would drop every payload
        without a word.
        """
        await self._seed(session, sessionmaker, 2)

        report = await run_details_backfill(
            FakeStratz(), sessionmaker, limit=10, source=RawSource.STRATZ_MATCH
        )

        assert report.fetched == 2
        async with sessionmaker() as check:
            assert await count_raw_matches(check, RawSource.STRATZ_MATCH) == 2
            assert await count_raw_matches(check, RawSource.OPENDOTA_MATCH) == 0

    async def test_the_default_source_is_unchanged(
        self, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """Existing callers must keep meaning what they meant; the switch is explicit."""
        await self._seed(session, sessionmaker, 1)

        await run_details_backfill(FakeOpenDota(), sessionmaker, limit=10)

        async with sessionmaker() as check:
            assert await count_raw_matches(check, RawSource.OPENDOTA_MATCH) == 1


class TestRawIdentity:
    """The raw layer is keyed on (match_id, source), and the two providers do not agree on
    what the id field is called. This is where that gets reconciled.
    """

    def test_match_id_is_read_from_either_provider(self) -> None:
        assert match_id_of({"match_id": 7}) == 7
        assert match_id_of({"id": 7}) == 7
        assert match_id_of({"match_id": None, "id": 7}) == 7

    def test_a_payload_with_no_id_is_reported_rather_than_guessed(self) -> None:
        assert match_id_of({"durationSeconds": 1800}) is None

    async def test_a_stratz_payload_lands_in_the_raw_layer(self, session: AsyncSession) -> None:
        written = await upsert_raw_matches(
            session, RawSource.STRATZ_MATCH, [{"id": 99, "durationSeconds": 1800}]
        )
        await session.commit()
        assert written == 1
        assert await count_raw_matches(session, RawSource.STRATZ_MATCH) == 1
