# Pro Segment — Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record Valve's league tier on every served prediction, hide amateur leagues from the live feed and the "played" feed, and compute the accuracy dashboard and the drift alert per segment (Tier 1 / Pro / Excluded).

**Architecture:** One rule (`app/domain/segments.py`) maps (Valve tier, Liquipedia tier) to a segment, in Python for the poller and the feeds and in SQL for the dashboard, with a parity test holding the two together. Valve tiers come from OpenDota `/leagues` into `leagues.valve_tier` via an hourly, throttled arq job that the poller can also request; the poller copies the tier onto each prediction at serve time. Filtering happens in the API, never in the poller, so excluded leagues keep being predicted and act as a control group.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 async + asyncpg, Alembic, arq 0.26, redis-py asyncio, pytest (asyncio_mode=auto), ruff, mypy strict.

**Spec:** `docs/superpowers/specs/2026-09-11-pro-segment-design.md` (read it first; this plan argues from it).

## Global Constraints

- Work on branch `feature/pro-segment` (already created from `development`, holds the spec commit). Never commit to or push `main`.
- Every command runs from `dota-oracle-backend/`. Tests, lint, alembic run in the tools container: `docker compose run --rm tools python -m pytest ...`. `docker compose up -d postgres redis` must be running for DB tests (they skip, not fail, without a DB — a skip is NOT a pass).
- Commit author is the repo-local `user.email` (`ersaim.adilet@yandex.kz`, already set). Commit messages: English, imperative, lowercase start (`add ...`, `record ...`). **No `Co-Authored-By` or any AI attribution trailer** — the project's CLAUDE.md forbids it.
- Code comments and docstrings in English, ASCII only (ruff `RUF001-003` flags Cyrillic look-alikes). Documentation (`CLAUDE.md`, `docs/spec.md`) is Russian.
- Segment names, exactly: `"tier1"`, `"pro"`, `"excluded"`. Valve tiers, exactly: `"premium"`, `"professional"`, `"amateur"`, `"excluded"`. Pro = `premium` or `professional`.
- Redis throttle key `valve_tiers:throttle`, TTL 900 s. arq job name and job id `refresh_valve_tiers`. Cron minute `3`.
- `predictions` is an append-only log (backend invariant 8): nothing rewrites an existing value; `backfill-segments` only fills NULLs.
- `/api/model/metrics` default segment is `tier1`.
- Before pushing, run exactly what CI runs, without `.env`:
  `docker compose run --rm -e STRATZ_API_TOKEN= -e STEAM_API_KEY= tools python -m pytest` and
  `docker compose run --rm tools sh -c "ruff check . && ruff format --check . && mypy app"`.

---

## File Structure

| File | Responsibility |
|---|---|
| `app/domain/segments.py` (create) | The segment rule: constants, `display_tier`, `segment_of`, `is_pro`, and their SQL twins `display_tier_expr`, `segment_expr` |
| `alembic/versions/3f8a1c2d9e41_pro_segment_columns.py` (create) | `leagues.valve_tier`, `predictions.league_id`, `predictions.valve_tier` |
| `app/db/models/reference.py`, `app/db/models/training.py` (modify) | ORM columns for the above |
| `app/ingestion/reference.py` (modify) | `parse_leagues` keeps Valve's tier |
| `app/ingestion/workers/valve_tiers.py` (create) | Throttled refresh job + enqueue helper |
| `app/workers/settings.py` (modify) | Register the job (keep_result=0) and its cron |
| `app/ingestion/workers/live_poller.py` (modify) | `LeagueContext`, prediction row with league/tier, feed entry `valve_tier`, request refresh |
| `app/schemas/common.py` (modify) | `LiveMatch.valve_tier`, `SegmentCount`, `ModelMetrics.segment/segments/unsegmented_matches` |
| `app/api/routes/matches.py` (modify) | `public_feed` gate on `/live`; `tiers` on `/recent` |
| `app/api/recent.py` (modify) | Pro gate, `tiers` filter, league from prediction, `display_tier` |
| `app/api/accuracy.py`, `app/api/routes/model.py` (modify) | Segment filter, `segment_counts` |
| `app/workers/drift.py` (modify) | `drift_verdicts` per (version, segment) |
| `app/ingestion/segments_backfill.py` (create), `app/ingestion/cli.py` (modify) | `backfill-segments` command |
| `CLAUDE.md`, `docs/spec.md` (modify) | Documentation |

---

### Task 1: The segment rule in Python

**Files:**
- Create: `app/domain/segments.py`
- Test: `tests/test_segments.py`

**Interfaces:**
- Produces (used by Tasks 2, 4, 5, 6, 7, 8):
  - `Segment = Literal["tier1", "pro", "excluded"]`
  - `SEGMENTS: tuple[Segment, ...] = ("tier1", "pro", "excluded")`
  - `PRO_VALVE_TIERS: tuple[str, ...] = ("professional", "premium")`
  - `EXCLUDED_VALVE_TIERS: tuple[str, ...] = ("excluded", "amateur")`
  - `VALVE_TIERS: tuple[str, ...]` (both of the above)
  - `display_tier(valve_tier: str | None, liquipedia_tier: str | None) -> str` — every function of the rule takes Valve's tier first
  - `segment_of(valve_tier: str | None, liquipedia_tier: str | None) -> Segment | None`
  - `is_pro(valve_tier: str | None) -> bool`

- [ ] **Step 1: Write the failing test**

Create `tests/test_segments.py`:

```python
"""The segment rule (design 2026-09-11-pro-segment, section 1).

Valve gates, Liquipedia ranks. Measured 11.09.2026: 30 of 32 Liquipedia Tier 1 leagues are
plain `professional` at Valve, exactly like 11 Tier 3 ones, and `premium` is The
International and almost nothing else - so Valve's tier can only say pro or not.
"""

import pytest

from app.domain.segments import display_tier, is_pro, segment_of


@pytest.mark.parametrize(
    ("valve", "liquipedia", "expected"),
    [
        ("professional", "tier1", "tier1"),
        ("premium", "tier1", "tier1"),
        # Section 3 fallback: an unmapped premium league is The International.
        ("premium", "unknown", "tier1"),
        ("premium", None, "tier1"),
        ("premium", "tier2", "pro"),
        ("professional", "unknown", "pro"),
        ("professional", None, "pro"),
        ("professional", "tier3", "pro"),
        # Valve gates first: markup cannot pull an amateur league into Tier 1.
        ("excluded", "tier1", "excluded"),
        ("amateur", None, "excluded"),
        # Unknown Valve tier is no segment at all, not a guess.
        (None, "tier1", None),
        (None, None, None),
        ("something-new", "tier1", None),
    ],
)
def test_segment_of(valve: str | None, liquipedia: str | None, expected: str | None) -> None:
    assert segment_of(valve, liquipedia) == expected


@pytest.mark.parametrize(
    ("valve", "liquipedia", "expected"),
    [
        ("professional", "tier1", "tier1"),
        ("premium", "tier3", "tier3"),
        ("premium", "unknown", "tier1"),
        ("premium", None, "tier1"),
        ("professional", None, "unknown"),
        (None, "unknown", "unknown"),
        ("excluded", "tier2", "tier2"),
    ],
)
def test_display_tier(valve: str | None, liquipedia: str | None, expected: str) -> None:
    # Same argument order as segment_of: Valve first. Both are `str | None`, so a swap would
    # type-check; one order everywhere is what keeps it from happening.
    assert display_tier(valve, liquipedia) == expected


@pytest.mark.parametrize(
    ("valve", "expected"),
    [
        ("premium", True),
        ("professional", True),
        ("excluded", False),
        ("amateur", False),
        (None, False),
    ],
)
def test_is_pro(valve: str | None, expected: bool) -> None:
    assert is_pro(valve) is expected
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose run --rm tools python -m pytest tests/test_segments.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.domain.segments'`

- [ ] **Step 3: Write minimal implementation**

Create `app/domain/segments.py`:

```python
"""Which slice of the world a prediction belongs to (design 2026-09-11-pro-segment).

Two sources, answering different questions. Valve's league tier (OpenDota `/leagues`)
separates professional from amateur and exists the moment a league does, but it cannot tell
Tier 1 from Tier 3: measured 11.09.2026, 30 of 32 Liquipedia Tier 1 leagues are plain
`professional`, like 11 Tier 3 ones, and `premium` is The International and almost nothing
else. Liquipedia separates Tier 1, but only for leagues somebody has mapped.

So Valve gates and Liquipedia ranks. The rule lives here once - in Python for the poller and
the feeds, and in SQL for the dashboard - and a test holds the two versions together.
"""

from typing import Literal

Segment = Literal["tier1", "pro", "excluded"]
SEGMENTS: tuple[Segment, ...] = ("tier1", "pro", "excluded")

PRO_VALVE_TIERS: tuple[str, ...] = ("professional", "premium")
EXCLUDED_VALVE_TIERS: tuple[str, ...] = ("excluded", "amateur")
VALVE_TIERS: tuple[str, ...] = PRO_VALVE_TIERS + EXCLUDED_VALVE_TIERS

LIQUIPEDIA_UNKNOWN = "unknown"


def display_tier(valve_tier: str | None, liquipedia_tier: str | None) -> str:
    """The tier a reader sees.

    Liquipedia's, except that an unmapped `premium` league reads as Tier 1: section 3 of the
    spec names this fallback, and without it the next International would sit under
    "unmarked" until somebody ran `map-leagues` by hand.
    """
    tier = liquipedia_tier or LIQUIPEDIA_UNKNOWN
    if tier == LIQUIPEDIA_UNKNOWN and valve_tier == "premium":
        return "tier1"
    return tier


def is_pro(valve_tier: str | None) -> bool:
    return valve_tier in PRO_VALVE_TIERS


def segment_of(valve_tier: str | None, liquipedia_tier: str | None) -> Segment | None:
    """Tier 1, Pro, Excluded - or None when Valve's tier is not known.

    None is not folded into any segment: a league missing from `/leagues` is a gap in our
    knowledge, and counting it as amateur or as pro would both be inventions.
    """
    if valve_tier in PRO_VALVE_TIERS:
        return "tier1" if display_tier(valve_tier, liquipedia_tier) == "tier1" else "pro"
    if valve_tier in EXCLUDED_VALVE_TIERS:
        return "excluded"
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose run --rm tools python -m pytest tests/test_segments.py -v`
Expected: PASS (25 passed)

- [ ] **Step 5: Commit**

```bash
git add app/domain/segments.py tests/test_segments.py
git commit -m "add the segment rule: Valve gates, Liquipedia ranks"
```

---

### Task 2: Columns, migration, and the rule in SQL

**Files:**
- Create: `alembic/versions/3f8a1c2d9e41_pro_segment_columns.py`
- Modify: `app/db/models/reference.py:26-40` (class `League`)
- Modify: `app/db/models/training.py:60-73` (class `Prediction`)
- Modify: `app/domain/segments.py`
- Test: `tests/test_segments.py`

**Interfaces:**
- Consumes: Task 1 constants and functions.
- Produces:
  - `League.valve_tier: Mapped[str | None]` (String(16))
  - `Prediction.league_id: Mapped[int | None]` (BigInteger, no FK), `Prediction.valve_tier: Mapped[str | None]` (String(16))
  - `display_tier_expr(valve_tier: ColumnElement[Any], liquipedia_tier: ColumnElement[Any]) -> ColumnElement[Any]`
  - `segment_expr(valve_tier: ColumnElement[Any], liquipedia_tier: ColumnElement[Any]) -> ColumnElement[Any]`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_segments.py`:

```python
from sqlalchemy import String, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.segments import display_tier_expr, segment_expr

VALVE_VALUES = [None, "premium", "professional", "amateur", "excluded", "something-new"]
LIQUIPEDIA_VALUES = [None, "unknown", "tier1", "tier2", "tier3"]


async def test_the_sql_rule_agrees_with_the_python_rule(session: AsyncSession) -> None:
    """Two copies of one rule. The dashboard reads the SQL one and the feed the Python one;
    if they drift apart, a match is Tier 1 on one page and Pro on the next."""
    for valve in VALVE_VALUES:
        for liquipedia in LIQUIPEDIA_VALUES:
            valve_param = literal(valve, String())
            liquipedia_param = literal(liquipedia, String())
            row = (
                await session.execute(
                    select(
                        segment_expr(valve_param, liquipedia_param),
                        display_tier_expr(valve_param, liquipedia_param),
                    )
                )
            ).one()
            assert row[0] == segment_of(valve, liquipedia), (valve, liquipedia)
            assert row[1] == display_tier(valve, liquipedia), (valve, liquipedia)
```

Move the new imports to the top import block of the file (ruff `I` will require it).

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose run --rm tools python -m pytest tests/test_segments.py -v`
Expected: FAIL — `ImportError: cannot import name 'display_tier_expr'`

- [ ] **Step 3: Add the SQL twins**

In `app/domain/segments.py` add imports at the top:

```python
from typing import Any, Literal

from sqlalchemy import ColumnElement, case, func
```

(replace the existing `from typing import Literal`), and append:

```python
def display_tier_expr(
    valve_tier: ColumnElement[Any], liquipedia_tier: ColumnElement[Any]
) -> ColumnElement[Any]:
    """`display_tier` in SQL. A LEFT JOIN that found no league yields NULL: read as unknown."""
    known = func.coalesce(liquipedia_tier, LIQUIPEDIA_UNKNOWN)
    return case(
        ((known == LIQUIPEDIA_UNKNOWN) & (valve_tier == "premium"), "tier1"),
        else_=known,
    )


def segment_expr(
    valve_tier: ColumnElement[Any], liquipedia_tier: ColumnElement[Any]
) -> ColumnElement[Any]:
    """`segment_of` in SQL. A NULL Valve tier falls through every branch to NULL."""
    return case(
        (
            valve_tier.in_(PRO_VALVE_TIERS)
            & (display_tier_expr(valve_tier, liquipedia_tier) == "tier1"),
            "tier1",
        ),
        (valve_tier.in_(PRO_VALVE_TIERS), "pro"),
        (valve_tier.in_(EXCLUDED_VALVE_TIERS), "excluded"),
        else_=None,
    )
```

- [ ] **Step 4: Add the ORM columns**

In `app/db/models/reference.py`, class `League`, after the `end_date` line add:

```python
    #: Valve's own label from OpenDota `/leagues`: premium, professional, amateur or excluded.
    #: Kept beside `tier`, never in it - `tier` is the hand-checked Liquipedia classification.
    #: It is the *current* label: Valve re-tags leagues over time, which is why predictions
    #: copy it at serve time instead of joining to it later.
    valve_tier: Mapped[str | None] = mapped_column(String(16))
```

In `app/db/models/training.py`, class `Prediction`, after the `features` line add:

```python
    #: The league as the poller saw it. Needed because an unscored match may have no row in
    #: `matches` at all yet (normalize invariant 13). No FK, like `match_id`: the log is written
    #: before the normalized layer knows the match, and must not depend on it.
    league_id: Mapped[int | None] = mapped_column(BigInteger)
    #: `leagues.valve_tier` at the moment the prediction was served. Never rewritten.
    valve_tier: Mapped[str | None] = mapped_column(String(16))
```

Check that `BigInteger` and `String` are already imported at the top of `training.py`; add them to the `sqlalchemy` import if not.

- [ ] **Step 5: Write the migration**

Create `alembic/versions/3f8a1c2d9e41_pro_segment_columns.py`:

```python
"""pro segment columns

Revision ID: 3f8a1c2d9e41
Revises: 174c4acce215
Create Date: 2026-09-12 10:00:00.000000

Schema only. Existing predictions are filled by `python -m app.ingestion.cli
backfill-segments`, which has to run after `reference` has loaded Valve tiers - at migration
time `leagues.valve_tier` is still empty.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3f8a1c2d9e41"
down_revision: str | None = "174c4acce215"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("leagues", sa.Column("valve_tier", sa.String(length=16), nullable=True))
    op.add_column("predictions", sa.Column("league_id", sa.BigInteger(), nullable=True))
    op.add_column("predictions", sa.Column("valve_tier", sa.String(length=16), nullable=True))


def downgrade() -> None:
    op.drop_column("predictions", "valve_tier")
    op.drop_column("predictions", "league_id")
    op.drop_column("leagues", "valve_tier")
```

- [ ] **Step 6: Run tests and the migration**

Run: `docker compose run --rm tools python -m pytest tests/test_segments.py -v`
Expected: PASS (26 passed, none skipped — if the parity test is SKIPPED, start postgres and rerun)

Run: `docker compose run --rm tools sh -c "alembic upgrade head && alembic downgrade -1 && alembic upgrade head && alembic current"`
Expected: ends with `3f8a1c2d9e41 (head)`. Never run `alembic downgrade base` on this database.

- [ ] **Step 7: Commit**

```bash
git add app/domain/segments.py app/db/models/reference.py app/db/models/training.py alembic/versions/3f8a1c2d9e41_pro_segment_columns.py tests/test_segments.py
git commit -m "record the league and its Valve tier on predictions"
```

---

### Task 3: Valve tiers from `/leagues`, on a throttled schedule

**Files:**
- Modify: `app/ingestion/reference.py:132-143` (`parse_leagues`), docstring of `refresh_league_names`
- Create: `app/ingestion/workers/valve_tiers.py`
- Modify: `app/workers/settings.py`
- Modify: `app/ingestion/cli.py:483` (help text of `reference`)
- Test: `tests/test_enrich_live.py` (class `TestLeagueNames`, helper `_FakeLeagues`)
- Test: `tests/ingestion/test_valve_tiers.py` (create)
- Test: `tests/workers/test_schedule.py`

**Interfaces:**
- Consumes: `VALVE_TIERS` (Task 1), `League.valve_tier` (Task 2), `refresh_league_names(client, session_factory) -> int` (existing), `RateLimitedError` from `app.ingestion.clients.base`.
- Produces (used by Task 4):
  - `REFRESH_VALVE_TIERS_JOB = "refresh_valve_tiers"`
  - `async def refresh_valve_tiers(ctx: dict[str, Any]) -> int`
  - `async def refresh_valve_tiers_once(client: LeagueSource, session_factory: Any, redis: Any) -> int | None` (None = skipped by throttle)
  - `async def request_valve_tier_refresh(ctx: dict[str, Any]) -> bool`

- [ ] **Step 1: Write the failing tests for `parse_leagues`**

In `tests/test_enrich_live.py`, change `_FakeLeagues.leagues` to return tiers:

```python
    async def leagues(self) -> list[dict[str, Any]]:
        if self._nameless:
            return [{"leagueid": 17599, "name": None, "tier": "professional"}]
        return [
            {"leagueid": 17599, "name": "Ultras Dota Pro League 2025-26", "tier": "excluded"},
            {"leagueid": 18445, "name": "Destiny League", "tier": "professional"},
        ]
```

Add `parse_leagues` to the import from `app.ingestion.reference`, and append to class `TestLeagueNames`:

```python
    async def test_stores_valve_tier_beside_the_liquipedia_one(self, session: AsyncSession) -> None:
        # Two columns, two owners: `map-leagues` owns `tier`, `/leagues` owns `valve_tier`.
        session.add(League(league_id=17599, tier="tier1"))
        await session.flush()
        await refresh_league_names(_FakeLeagues(), lambda: _Once(session))
        league = (
            await session.execute(select(League).where(League.league_id == 17599))
        ).scalar_one()
        assert league.tier == "tier1"
        assert league.valve_tier == "excluded"

    async def test_a_tier_valve_withdraws_is_cleared(self, session: AsyncSession) -> None:
        # The column holds the current label; a stale "professional" would keep an amateur
        # league in the feed.
        session.add(League(league_id=18445, valve_tier="professional"))
        await session.flush()

        class _NoTier(_FakeLeagues):
            async def leagues(self) -> list[dict[str, Any]]:
                return [{"leagueid": 18445, "name": "Destiny League", "tier": None}]

        await refresh_league_names(_NoTier(), lambda: _Once(session))
        tier = await session.scalar(select(League.valve_tier).where(League.league_id == 18445))
        assert tier is None

    def test_an_unrecognised_valve_tier_is_not_stored(self) -> None:
        rows = parse_leagues([{"leagueid": 1, "name": "x", "tier": "legendary"}])
        assert rows[0]["valve_tier"] is None
```

- [ ] **Step 2: Run to verify failure**

Run: `docker compose run --rm tools python -m pytest tests/test_enrich_live.py -v -k LeagueNames`
Expected: FAIL — `KeyError: 'valve_tier'` / `assert None == 'excluded'`

- [ ] **Step 3: Implement `parse_leagues`**

In `app/ingestion/reference.py` add `from app.domain.segments import VALVE_TIERS` to the imports and replace `parse_leagues` with:

```python
def parse_leagues(payload: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`/leagues` gives an id, a name and Valve's own tier for every league OpenDota knows.

    The tier goes to `valve_tier`, never to `tier`: `tier` is the Liquipedia classification
    that `map-leagues` decides and a human checks, and it must survive this pass untouched.
    Valve's label is what separates professional leagues from amateur ones in the live feed
    (design 2026-09-11-pro-segment). An unrecognised value is stored as unknown, not guessed.
    """
    rows: list[dict[str, Any]] = []
    for row in payload:
        league_id = row.get("leagueid")
        name = row.get("name")
        if not league_id or not name:
            continue
        tier = row.get("tier")
        rows.append(
            {
                "league_id": int(league_id),
                "name": name,
                "valve_tier": tier if tier in VALVE_TIERS else None,
            }
        )
    return rows
```

In the docstring of `refresh_league_names`, replace the paragraph starting `Names only.` with:

```
    Names and Valve's tier only. The Liquipedia tier, slug, prize pool and dates belong to
    `map-leagues`, which writes exactly those columns and never `name` or `valve_tier` - so
    refreshing here cannot undo hand-checked classification work.
```

In `app/ingestion/cli.py` change the `reference` help text to:

```python
    sub.add_parser(
        "reference", help="load heroes, pro-player names and leagues with Valve tiers (3 calls)"
    )
```

- [ ] **Step 4: Run to verify pass**

Run: `docker compose run --rm tools python -m pytest tests/test_enrich_live.py -v`
Expected: PASS (all, including the three new tests)

- [ ] **Step 5: Write the failing tests for the job**

Create `tests/ingestion/test_valve_tiers.py`:

```python
"""Keeping Valve's league tiers fresh without spending OpenDota's quota on it.

A new tournament has no tier until `/leagues` is asked again, and until then the feed hides
it. The poller may therefore ask for a refresh every tick - the throttle is what turns that
into at most four calls an hour.
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.reference import League
from app.ingestion.clients.base import RateLimitedError
from app.ingestion.workers.valve_tiers import (
    REFRESH_EVERY_SECONDS,
    REFRESH_VALVE_TIERS_JOB,
    THROTTLE_KEY,
    refresh_valve_tiers,
    refresh_valve_tiers_once,
    request_valve_tier_refresh,
)


class _FakeRedis:
    """Only `SET key value NX EX ttl`, which is all the throttle uses."""

    def __init__(self) -> None:
        self.store: dict[str, tuple[str, int]] = {}

    async def set(self, name: str, value: str, *, nx: bool = False, ex: int | None = None) -> Any:
        if nx and name in self.store:
            return None
        self.store[name] = (value, ex or 0)
        return True


class _Leagues:
    def __init__(self, *, fail: Exception | None = None) -> None:
        self.calls = 0
        self._fail = fail

    async def leagues(self) -> list[dict[str, Any]]:
        self.calls += 1
        if self._fail:
            raise self._fail
        return [{"leagueid": 19696, "name": "DreamLeague Season 29", "tier": "professional"}]


class _Once:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def __aenter__(self) -> AsyncSession:
        return self._session

    async def __aexit__(self, *_: object) -> None:
        return None


class _FakeArq:
    def __init__(self, *, result: object = object(), window_held: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._result = result
        self._held = window_held

    async def exists(self, name: str) -> int:
        return int(self._held and name == THROTTLE_KEY)

    async def enqueue_job(self, name: str, **kwargs: Any) -> object:
        self.calls.append((name, kwargs))
        return self._result


async def test_writes_the_valve_tier(session: AsyncSession) -> None:
    session.add(League(league_id=19696))
    await session.flush()

    written = await refresh_valve_tiers_once(_Leagues(), lambda: _Once(session), _FakeRedis())

    assert written == 1
    tier = await session.scalar(select(League.valve_tier).where(League.league_id == 19696))
    assert tier == "professional"


async def test_a_second_call_inside_the_window_is_skipped(session: AsyncSession) -> None:
    client, redis = _Leagues(), _FakeRedis()

    first = await refresh_valve_tiers_once(client, lambda: _Once(session), redis)
    second = await refresh_valve_tiers_once(client, lambda: _Once(session), redis)

    assert first == 1
    assert second is None
    assert client.calls == 1
    assert redis.store[THROTTLE_KEY][1] == REFRESH_EVERY_SECONDS


async def test_a_refusal_stops_quietly_and_still_holds_the_window(session: AsyncSession) -> None:
    # Retrying a 429 sooner is how an IP ban is earned; the throttle key stays set.
    client, redis = _Leagues(fail=RateLimitedError(60)), _FakeRedis()

    assert await refresh_valve_tiers_once(client, lambda: _Once(session), redis) == 0
    assert await refresh_valve_tiers_once(client, lambda: _Once(session), redis) is None
    assert client.calls == 1


async def test_any_other_failure_does_not_raise(session: AsyncSession) -> None:
    client = _Leagues(fail=RuntimeError("boom"))
    assert await refresh_valve_tiers_once(client, lambda: _Once(session), _FakeRedis()) == 0


async def test_the_request_uses_a_fixed_job_id() -> None:
    # A fixed id is what stops thirty ticks from queueing thirty refreshes.
    arq = _FakeArq()
    assert await request_valve_tier_refresh({"redis": arq}) is True
    assert arq.calls == [(REFRESH_VALVE_TIERS_JOB, {"_job_id": REFRESH_VALVE_TIERS_JOB})]


async def test_an_already_queued_request_is_reported_as_not_queued() -> None:
    assert await request_valve_tier_refresh({"redis": _FakeArq(result=None)}) is False


async def test_a_closed_window_is_not_even_queued() -> None:
    # Without this, a league missing from `/leagues` queues the job every tick: 120 runs an
    # hour, each logging "skipped" - a light that is always on, which hides the real one.
    arq = _FakeArq(window_held=True)
    assert await request_valve_tier_refresh({"redis": arq}) is False
    assert arq.calls == []


async def test_no_redis_in_the_context_is_not_an_error() -> None:
    assert await request_valve_tier_refresh({}) is False


def test_the_job_id_is_the_function_name() -> None:
    # arq resolves a queued job by function name; the two must not drift apart.
    assert refresh_valve_tiers.__name__ == REFRESH_VALVE_TIERS_JOB
```

Append to `tests/workers/test_schedule.py`:

```python
def test_valve_tiers_refresh_hourly_and_can_be_requested_again_soon() -> None:
    """arq refuses to enqueue a job id whose result is still stored - an hour by default -
    so the poller's request would be dead for an hour after every run. keep_result=0 is what
    lets the throttle, not arq, decide how often it runs."""
    from app.ingestion.workers.valve_tiers import REFRESH_VALVE_TIERS_JOB, refresh_valve_tiers

    assert _job(refresh_valve_tiers).minute == 3
    registered = [
        function
        for function in WorkerSettings.functions
        if getattr(function, "name", None) == REFRESH_VALVE_TIERS_JOB
    ]
    assert len(registered) == 1
    assert registered[0].keep_result_s == 0
```

- [ ] **Step 6: Run to verify failure**

Run: `docker compose run --rm tools python -m pytest tests/ingestion/test_valve_tiers.py tests/workers/test_schedule.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.ingestion.workers.valve_tiers'`

- [ ] **Step 7: Implement the job**

Create `app/ingestion/workers/valve_tiers.py`:

```python
"""Valve's league tiers, refreshed hourly and on request (design 2026-09-11-pro-segment).

The live feed shows only professional leagues, and a league's Valve tier is known only once
`/leagues` has been asked since the league appeared. Hourly alone would hide a new Tier 1
tournament for up to an hour - a whole map - so the poller may also request a refresh.

It may want one every tick, for a league OpenDota simply does not list. The throttle is
therefore inside the job, keyed in Redis, and holds whoever asked: at most one call per
`REFRESH_EVERY_SECONDS`. The request checks the same key before queueing, so a closed window
costs one EXISTS per tick rather than a job run and a log line. `/leagues` is a megabyte, which is also why the poller enqueues this
rather than calling it inside its thirty-second tick.
"""

from typing import Any

from app.core.logging import get_logger
from app.core.redis import get_redis
from app.db.session import get_session_factory
from app.ingestion.clients.base import RateLimitedError
from app.ingestion.clients.opendota import OpenDotaClient
from app.ingestion.reference import LeagueSource, refresh_league_names

log = get_logger(__name__)

REFRESH_VALVE_TIERS_JOB = "refresh_valve_tiers"
THROTTLE_KEY = "valve_tiers:throttle"
REFRESH_EVERY_SECONDS = 15 * 60


async def refresh_valve_tiers_once(
    client: LeagueSource, session_factory: Any, redis: Any
) -> int | None:
    """Leagues written, 0 on failure, or None when the window is still closed.

    The throttle key is claimed before the call and left in place on failure: a refusal is
    exactly when asking again sooner does harm.
    """
    if not await redis.set(THROTTLE_KEY, "1", nx=True, ex=REFRESH_EVERY_SECONDS):
        log.info("valve_tiers.skipped")
        return None
    try:
        return await refresh_league_names(client, session_factory)
    except RateLimitedError as exc:
        log.warning("valve_tiers.rate_limited", retry_after=exc.retry_after)
    except Exception as exc:
        log.warning("valve_tiers.failed", error=str(exc))
    return 0


async def refresh_valve_tiers(ctx: dict[str, Any]) -> int:
    """arq entry point, cron and on request alike."""
    async with OpenDotaClient() as client:
        written = await refresh_valve_tiers_once(client, get_session_factory(), get_redis())
    return written or 0


async def request_valve_tier_refresh(ctx: dict[str, Any]) -> bool:
    """Ask the worker for a refresh. True when a job was queued.

    Nothing is queued while the throttle window is closed: a league OpenDota does not list
    would otherwise queue a job every tick, and the worker would run it 120 times an hour only
    to log "skipped". The fixed job id makes arq drop duplicates while one is waiting. arq's
    pool and `get_redis()` point at the same database (host, port and db from one settings
    object), so the key the job sets is the key checked here.
    """
    redis = ctx.get("redis")
    if redis is None:
        return False
    try:
        if await redis.exists(THROTTLE_KEY):
            return False
        job = await redis.enqueue_job(REFRESH_VALVE_TIERS_JOB, _job_id=REFRESH_VALVE_TIERS_JOB)
    except Exception as exc:
        log.warning("valve_tiers.enqueue_failed", error=str(exc))
        return False
    return job is not None
```

- [ ] **Step 8: Register the job**

In `app/workers/settings.py`:

- add `from arq.worker import func` next to `from arq.cron import cron`;
- add `from app.ingestion.workers.valve_tiers import refresh_valve_tiers` to the app imports (keep isort order);
- in `functions`, after `sync_liquipedia,` add:

```python
        # keep_result=0: arq refuses a job id whose result is still stored (an hour by
        # default), which would silence the poller's request for an hour after every run.
        func(refresh_valve_tiers, keep_result=0, max_tries=1),
```

- in `cron_jobs`, after the `sync_liquipedia` cron add:

```python
            # Valve's league tiers gate the live feed (design 2026-09-11-pro-segment). Hourly,
            # plus on request from the poller when a live league has none; the job throttles
            # itself to one `/leagues` call per fifteen minutes whoever asks.
            cron(refresh_valve_tiers, minute=3, max_tries=1),
```

- [ ] **Step 9: Run to verify pass**

Run: `docker compose run --rm tools python -m pytest tests/ingestion/test_valve_tiers.py tests/workers/test_schedule.py tests/test_enrich_live.py -v`
Expected: PASS

- [ ] **Step 10: Commit**

```bash
git add app/ingestion/reference.py app/ingestion/workers/valve_tiers.py app/workers/settings.py app/ingestion/cli.py tests/test_enrich_live.py tests/ingestion/test_valve_tiers.py tests/workers/test_schedule.py
git commit -m "keep Valve league tiers fresh with a throttled refresh job"
```

---

### Task 4: The poller records the segment and asks for missing tiers

**Files:**
- Modify: `app/ingestion/workers/live_poller.py`
- Modify: `app/schemas/common.py:45-75` (class `LiveMatch`)
- Test: `tests/ingestion/test_live_poller.py`

**Interfaces:**
- Consumes: `display_tier` (Task 1), `Prediction.league_id/valve_tier`, `League.valve_tier` (Task 2), `request_valve_tier_refresh` (Task 3).
- Produces:
  - `class LeagueContext(NamedTuple)`: `name: str | None`, `tier: str`, `fmt: SeriesFormat | None`, `is_lan: bool | None`, `valve_tier: str | None`
  - `UNKNOWN_LEAGUE: LeagueContext`
  - `leagues_without_valve_tier(contexts: Mapping[int, LeagueContext], league_ids: set[int]) -> set[int]`
  - `prediction_row(*, match_id: int, minute: int, captured_at: datetime, model_version: str, p_radiant: float, features: dict[str, Any], league_id: int, context: LeagueContext) -> Prediction`
  - `_feed_entry(..., valve_tier: str | None = None)`; feed entries carry `"valve_tier"` and `"tier" = display_tier(...)`
  - `LiveMatch.valve_tier: str | None` (used by Task 5)

- [ ] **Step 1: Write the failing tests**

In `tests/ingestion/test_live_poller.py`:

Replace the import line `from app.ingestion.workers.live_poller import _feed_entry, _series_context` with:

```python
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.reference import League
from app.ingestion.workers.live_poller import (
    UNKNOWN_LEAGUE,
    LeagueContext,
    _feed_entry,
    _league_context,
    _series_context,
    leagues_without_valve_tier,
    prediction_row,
)
```

(keep the imports sorted: stdlib, third-party, app).

Replace `TestFeedEntry._entry` with:

```python
    def _entry(
        self,
        *,
        known: bool,
        fmt: SeriesFormat | None = SeriesFormat.BO3,
        tier: str = "tier1",
        valve_tier: str | None = "professional",
    ) -> dict[str, Any]:
        game = live_game(radiant_wins=1, dire_wins=0)
        return _feed_entry(
            game,
            p_radiant=0.62,
            model_version="baseline-logistic-0.1",
            minute=20,
            league_name="DreamLeague Season 29",
            tier=tier,
            series=_series_context(game, fmt),
            series_format_known=known,
            team_history=0,
            valve_tier=valve_tier,
        )
```

Add `"valve_tier"` to the `parametrize` list of `test_identifying_fields_are_present`, and append to `TestFeedEntry`:

```python
    def test_carries_the_valve_tier_for_the_api_gate(self) -> None:
        assert self._entry(known=True, valve_tier="excluded")["valve_tier"] == "excluded"

    def test_an_unmapped_premium_league_reads_as_tier1(self) -> None:
        # Section 3 fallback: otherwise the next International hides under "unmarked".
        entry = self._entry(known=True, tier="unknown", valve_tier="premium")
        assert entry["tier"] == "tier1"

    def test_liquipedia_tier_wins_when_there_is_one(self) -> None:
        assert self._entry(known=True, tier="tier3", valve_tier="premium")["tier"] == "tier3"
```

Append new test classes at the end of the file:

```python
class TestValveTierRequest:
    def test_names_the_leagues_without_a_valve_tier(self) -> None:
        contexts = {
            1: UNKNOWN_LEAGUE._replace(valve_tier="professional"),
            2: UNKNOWN_LEAGUE,
        }
        # 3 has no context at all: the poller registers it, but a missing entry still counts.
        assert leagues_without_valve_tier(contexts, {1, 2, 3}) == {2, 3}

    def test_nothing_to_ask_when_every_league_is_known(self) -> None:
        contexts = {1: UNKNOWN_LEAGUE._replace(valve_tier="excluded")}
        assert leagues_without_valve_tier(contexts, {1}) == set()


class TestPredictionRow:
    def test_records_the_league_and_its_valve_tier_at_serve_time(self) -> None:
        context = LeagueContext(
            name="DreamLeague Season 29",
            tier="tier1",
            fmt=None,
            is_lan=None,
            valve_tier="professional",
        )
        row = prediction_row(
            match_id=8968034437,
            minute=20,
            captured_at=datetime(2026, 9, 12, tzinfo=UTC),
            model_version="lgbm-20260901-102407",
            p_radiant=0.62,
            features={"minute": 20.0},
            league_id=19696,
            context=context,
        )
        assert (row.league_id, row.valve_tier) == (19696, "professional")
        assert (row.match_id, row.minute, row.p_radiant) == (8968034437, 20, 0.62)

    def test_league_zero_is_stored_as_unknown(self) -> None:
        row = prediction_row(
            match_id=1,
            minute=1,
            captured_at=datetime(2026, 9, 12, tzinfo=UTC),
            model_version="v",
            p_radiant=0.5,
            features={},
            league_id=0,
            context=UNKNOWN_LEAGUE,
        )
        assert row.league_id is None
        assert row.valve_tier is None


class TestLeagueContext:
    async def test_reads_the_valve_tier(self, session: AsyncSession) -> None:
        session.add(League(league_id=19696, name="DreamLeague Season 29", valve_tier="premium"))
        await session.flush()

        contexts = await _league_context(session, {19696})

        assert contexts[19696].valve_tier == "premium"
        assert contexts[19696].name == "DreamLeague Season 29"
```

- [ ] **Step 2: Run to verify failure**

Run: `docker compose run --rm tools python -m pytest tests/ingestion/test_live_poller.py -v`
Expected: FAIL — `ImportError: cannot import name 'UNKNOWN_LEAGUE'`

- [ ] **Step 3: Implement in the poller**

In `app/ingestion/workers/live_poller.py`:

Imports — change `from typing import Any` to:

```python
from collections.abc import Mapping
from typing import Any, NamedTuple
```

and add:

```python
from app.domain.segments import display_tier
from app.ingestion.workers.valve_tiers import request_valve_tier_refresh
```

After `LIVE_FEED_TTL = 120` add:

```python
class LeagueContext(NamedTuple):
    """What the league tables know about a live game's league."""

    name: str | None
    #: Liquipedia tier, `unknown` until mapped.
    tier: str
    fmt: SeriesFormat | None
    is_lan: bool | None
    #: Valve's tier from `/leagues`; None until the refresh job has seen the league.
    valve_tier: str | None


UNKNOWN_LEAGUE = LeagueContext(name=None, tier="unknown", fmt=None, is_lan=None, valve_tier=None)
```

Replace `_league_context` with:

```python
async def _league_context(session: AsyncSession, league_ids: set[int]) -> dict[int, LeagueContext]:
    """Name, tiers, current stage format and LAN flag per league.

    `is_lan` is here because the feature vector needs it and only this table has it. It stays
    None for an unmapped league, and the vector reports that rather than defaulting to
    "online" - the same rule the format follows. `valve_tier` is what the API gates the feed
    on, and what each prediction records.
    """
    if not league_ids:
        return {}

    leagues = {
        int(league_id): (name, str(tier), is_lan, valve_tier)
        for league_id, name, tier, is_lan, valve_tier in (
            await session.execute(
                select(
                    League.league_id, League.name, League.tier, League.is_lan, League.valve_tier
                ).where(League.league_id.in_(league_ids))
            )
        ).all()
    }

    now = datetime.now(UTC)
    formats: dict[int, SeriesFormat] = {}
    for league_id, default_format, starts_at, ends_at in (
        await session.execute(
            select(
                TournamentStage.league_id,
                TournamentStage.default_format,
                TournamentStage.starts_at,
                TournamentStage.ends_at,
            ).where(TournamentStage.league_id.in_(league_ids))
        )
    ).all():
        # The stage running today is the one this map belongs to.
        if starts_at and ends_at and starts_at <= now <= ends_at:
            formats[int(league_id)] = SeriesFormat(str(default_format))

    return {
        league_id: LeagueContext(
            name=name,
            tier=tier,
            fmt=formats.get(league_id),
            is_lan=is_lan,
            valve_tier=valve_tier,
        )
        for league_id, (name, tier, is_lan, valve_tier) in leagues.items()
    }


def leagues_without_valve_tier(
    contexts: Mapping[int, LeagueContext], league_ids: set[int]
) -> set[int]:
    """Live leagues the feed would hide for want of a Valve tier."""
    return {
        league_id
        for league_id in league_ids
        if contexts.get(league_id, UNKNOWN_LEAGUE).valve_tier is None
    }


def prediction_row(
    *,
    match_id: int,
    minute: int,
    captured_at: datetime,
    model_version: str,
    p_radiant: float,
    features: dict[str, Any],
    league_id: int,
    context: LeagueContext,
) -> Prediction:
    """The log row. The Valve tier is copied now, not joined later: Valve re-tags leagues
    over time (34% of the archive sits in leagues that are `excluded` today), and a join
    would move old predictions between dashboard segments behind everybody's back."""
    return Prediction(
        match_id=match_id,
        minute=minute,
        predicted_at=captured_at,
        model_version=model_version,
        p_radiant=p_radiant,
        features=features,
        league_id=league_id or None,
        valve_tier=context.valve_tier,
    )
```

In `_feed_entry`, add the parameter `valve_tier: str | None = None,` after `team_history: int,`, and in the returned dict replace `"tier": tier,` with:

```python
        # What the reader sees, with the section 3 premium fallback applied.
        "tier": display_tier(valve_tier, tier),
        # What the API gates on. The poller keeps every game in the cache; `/matches/live`
        # decides what is public (design 2026-09-11-pro-segment).
        "valve_tier": valve_tier,
```

In `poll_live_games`, right after `contexts = await _league_context(session, league_ids)` add:

```python
        if leagues_without_valve_tier(contexts, league_ids):
            await request_valve_tier_refresh(ctx)
```

Replace the line
`league_name, tier, fmt, is_lan = contexts.get(league_id, (None, "unknown", None, None))`
and the next line `series = _series_context(game, fmt)` with:

```python
            context = contexts.get(league_id, UNKNOWN_LEAGUE)
            series = _series_context(game, context.fmt)
```

In the `from_live_league_game(...)` call replace `is_lan=is_lan,` with `is_lan=context.is_lan,`.

Replace the `session.add(Prediction(...))` block with:

```python
            session.add(
                prediction_row(
                    match_id=match_id,
                    minute=state.minute,
                    captured_at=captured_at,
                    model_version=predictor.version,
                    p_radiant=p_radiant,
                    features=features,
                    league_id=league_id,
                    context=context,
                )
            )
```

Replace the `_feed_entry(...)` call arguments `league_name, tier, series, series_format_known=fmt is not None,` with `context.name, context.tier, series, series_format_known=context.fmt is not None,` and add `valve_tier=context.valve_tier,` after the `team_history=notability(...)` argument.

- [ ] **Step 4: Add the schema field**

In `app/schemas/common.py`, class `LiveMatch`, after `tier: str = "unknown"` add:

```python
    valve_tier: str | None = Field(
        default=None,
        description=(
            "Valve's league tier from OpenDota /leagues. The public feed shows only "
            "professional and premium leagues; null means the tier is not known yet."
        ),
    )
```

- [ ] **Step 5: Run to verify pass**

Run: `docker compose run --rm tools python -m pytest tests/ingestion/test_live_poller.py tests/test_live_notability.py -v`
Expected: PASS

Run: `docker compose run --rm tools mypy app`
Expected: `Success: no issues found`

- [ ] **Step 6: Commit**

```bash
git add app/ingestion/workers/live_poller.py app/schemas/common.py tests/ingestion/test_live_poller.py
git commit -m "record the Valve tier on each prediction and request missing tiers"
```

---

### Task 5: The public live feed shows only professional leagues

**Files:**
- Modify: `app/api/routes/matches.py:38-52`
- Test: `tests/test_live_feed.py` (create)

**Interfaces:**
- Consumes: `is_pro` (Task 1), feed entries with `valve_tier` and display `tier` (Task 4).
- Produces: `public_feed(entries: list[dict[str, Any]], tier: str | None) -> list[dict[str, Any]]`

- [ ] **Step 1: Write the failing test**

Create `tests/test_live_feed.py`:

```python
"""The public live feed (design 2026-09-11-pro-segment, section 2).

The product promises Tier 1 predictions. Measured on production 11.09.2026: every one of the
249 scored live predictions came from a league Valve tags `excluded`. The poller keeps
predicting those - they are the control group - but the feed does not show them.
"""

from typing import Any

from app.api.routes.matches import public_feed


def entry(match_id: int, valve_tier: str | None, tier: str = "unknown") -> dict[str, Any]:
    return {"match_id": match_id, "valve_tier": valve_tier, "tier": tier}


def test_hides_amateur_and_unknown_leagues() -> None:
    feed = [
        entry(1, "professional"),
        entry(2, "excluded"),
        entry(3, None),
        entry(4, "premium", "tier1"),
        entry(5, "amateur"),
    ]
    assert [e["match_id"] for e in public_feed(feed, None)] == [1, 4]


def test_keeps_the_order_the_poller_wrote() -> None:
    feed = [entry(9, "professional"), entry(3, "professional"), entry(7, "premium")]
    assert [e["match_id"] for e in public_feed(feed, None)] == [9, 3, 7]


def test_the_tier_filter_applies_on_top_of_the_gate() -> None:
    feed = [
        entry(1, "professional", "tier1"),
        entry(2, "excluded", "tier1"),
        entry(3, "professional", "tier3"),
    ]
    assert [e["match_id"] for e in public_feed(feed, "tier1")] == [1]


def test_an_entry_cached_before_the_field_existed_is_hidden() -> None:
    # The cache lives 120 seconds; for that long after a deploy it holds entries without the
    # key. Hiding them is the honest reading of "tier unknown".
    assert public_feed([{"match_id": 1, "tier": "tier1"}], None) == []
```

- [ ] **Step 2: Run to verify failure**

Run: `docker compose run --rm tools python -m pytest tests/test_live_feed.py -v`
Expected: FAIL — `ImportError: cannot import name 'public_feed'`

- [ ] **Step 3: Implement**

In `app/api/routes/matches.py` add imports `from typing import Any` and `from app.domain.segments import is_pro`, then replace the `live_matches` function with:

```python
def public_feed(entries: list[dict[str, Any]], tier: str | None) -> list[dict[str, Any]]:
    """The part of the poller's feed the product shows.

    Only professional and premium leagues: the service promises Tier 1 predictions, and an
    empty feed at four in the morning is honest where a feed of amateur cups is not. The
    poller keeps predicting the rest - the accuracy dashboard uses them as a control group -
    so the gate lives here, not there. `tier` narrows further and is compared with the
    display tier the poller wrote.
    """
    visible = [entry for entry in entries if is_pro(entry.get("valve_tier"))]
    if tier:
        visible = [entry for entry in visible if entry.get("tier") == tier]
    return visible


@router.get("/live", response_model=list[LiveMatch])
async def live_matches(tier: str | None = None) -> list[LiveMatch]:
    """Whatever the poller last saw, minus leagues outside the professional scene.

    Served from the cache the poller writes rather than recomputed, and that cache expires
    after two minutes: an empty feed is honest, a stale one looks live and is not.
    """
    cached = await get_redis().get(LIVE_FEED_KEY)
    if not cached:
        return []
    return [LiveMatch.model_validate(entry) for entry in public_feed(orjson.loads(cached), tier)]
```

- [ ] **Step 4: Run to verify pass**

Run: `docker compose run --rm tools python -m pytest tests/test_live_feed.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add app/api/routes/matches.py tests/test_live_feed.py
git commit -m "show only professional leagues in the public live feed"
```

---

### Task 6: The "played" feed — pro gate and tier filter

**Files:**
- Modify: `app/api/recent.py:61-193` (`recent_matches`)
- Modify: `app/api/routes/matches.py:55-64` (`recent` route)
- Test: `tests/test_recent.py`

**Interfaces:**
- Consumes: `PRO_VALVE_TIERS`, `display_tier`, `display_tier_expr` (Tasks 1-2), `Prediction.league_id/valve_tier` (Task 2).
- Produces: `recent_matches(session: AsyncSession, limit: int = 20, tiers: Sequence[str] | None = None) -> list[RecentMatch]`; route `GET /api/matches/recent?limit=&tiers=tier1,unknown` (422 on an unknown tier).

- [ ] **Step 1: Write the failing tests**

In `tests/test_recent.py`:

Add `from app.db.models.reference import League` to the imports.

Replace the `add` helper with (existing tests keep passing because the default tier is professional):

```python
async def add(
    session: AsyncSession,
    match_id: int,
    radiant_win: bool | None,
    minutes: list[int],
    *,
    start: datetime = BASE,
    valve_tier: str | None = "professional",
    league_id: int | None = None,
) -> None:
    session.add(Match(match_id=match_id, radiant_win=radiant_win, start_time=start))
    await session.flush()
    for minute in minutes:
        session.add(
            Prediction(
                match_id=match_id,
                minute=minute,
                predicted_at=start + timedelta(minutes=minute),
                model_version=VERSION,
                p_radiant=0.5 + minute / 200,
                features={},
                league_id=league_id,
                valve_tier=valve_tier,
            )
        )
    await session.flush()
```

Append to `TestRecentMatches`:

```python
    async def test_hides_a_match_predicted_in_an_excluded_league(
        self, session: AsyncSession
    ) -> None:
        await add(session, 1, True, [10], valve_tier="excluded")
        assert await recent_matches(session) == []

    async def test_hides_a_match_whose_valve_tier_was_unknown(self, session: AsyncSession) -> None:
        await add(session, 1, True, [10], valve_tier=None)
        assert await recent_matches(session) == []

    async def test_filters_by_display_tier(self, session: AsyncSession) -> None:
        session.add(League(league_id=100, name="DreamLeague Season 29", tier="tier1"))
        await session.flush()
        await add(session, 1, True, [10], league_id=100)
        await add(session, 2, True, [10], start=BASE + timedelta(hours=1))

        assert [r.match_id for r in await recent_matches(session, tiers=["tier1"])] == [1]
        assert [r.match_id for r in await recent_matches(session, tiers=["unknown"])] == [2]
        assert len(await recent_matches(session, tiers=["tier1", "unknown"])) == 2

    async def test_an_unmapped_premium_league_is_tier1(self, session: AsyncSession) -> None:
        session.add(League(league_id=200, name="The International 2027"))
        await session.flush()
        await add(session, 1, True, [10], valve_tier="premium", league_id=200)

        rows = await recent_matches(session, tiers=["tier1"])

        assert [r.match_id for r in rows] == [1]
        assert rows[0].tier == "tier1"

    async def test_takes_the_league_from_the_prediction_when_the_match_has_none(
        self, session: AsyncSession
    ) -> None:
        # A skeleton match from a detail payload has no league_id (normalize invariant 13).
        session.add(League(league_id=100, name="DreamLeague Season 29", tier="tier1"))
        await session.flush()
        await add(session, 1, True, [10], league_id=100)

        row = (await recent_matches(session))[0]

        assert row.league_id == 100
        assert row.league_name == "DreamLeague Season 29"
        assert row.tier == "tier1"
```

Append to `TestRoute`:

```python
    def test_refuses_an_unknown_tier(self, client: TestClient) -> None:
        response = client.get("/api/matches/recent", params={"tiers": "tier1,tier9"})
        assert response.status_code == 422
```

- [ ] **Step 2: Run to verify failure**

Run: `docker compose run --rm tools python -m pytest tests/test_recent.py -v`
Expected: FAIL — `TypeError: recent_matches() got an unexpected keyword argument 'tiers'` and the excluded/unknown tests failing.

- [ ] **Step 3: Implement `recent_matches`**

In `app/api/recent.py` add imports:

```python
from collections.abc import Sequence

from app.domain.segments import PRO_VALVE_TIERS, display_tier, display_tier_expr
```

Update the module docstring's last sentence to add: `Only matches predicted in professional leagues appear (design 2026-09-11-pro-segment).`

Replace the signature and the `match_ids = list(...)` block at the top of `recent_matches` with:

```python
async def recent_matches(
    session: AsyncSession, limit: int = 20, tiers: Sequence[str] | None = None
) -> list[RecentMatch]:
    """Finished matches we predicted in professional leagues, newest first.

    `tiers` filters on the display tier - Liquipedia's, with an unmapped `premium` league
    reading as Tier 1 - and is applied here rather than in the browser: twenty newest matches
    can hold no Tier 1 at all while older ones do.
    """
    # A skeleton match has no league; the poller wrote the one it saw on every prediction.
    league_id = func.coalesce(Match.league_id, Prediction.league_id)
    statement = (
        select(Match.match_id)
        .join(Prediction, Prediction.match_id == Match.match_id)
        .outerjoin(League, League.league_id == league_id)
        .where(Match.radiant_win.is_not(None), Prediction.valve_tier.in_(PRO_VALVE_TIERS))
        .group_by(Match.match_id, Match.start_time)
        .order_by(Match.start_time.desc().nulls_last(), Match.match_id.desc())
        .limit(limit)
    )
    if tiers:
        statement = statement.where(
            display_tier_expr(Prediction.valve_tier, League.tier).in_(list(tiers))
        )
    match_ids = list((await session.scalars(statement)).all())
```

Right after the `matches = {...}` dict, add:

```python
    # One league and one Valve tier per match: all its predictions come from the same league.
    predicted_league: dict[int, tuple[int | None, str | None]] = {
        int(match_id): (league, valve)
        for match_id, league, valve in (
            await session.execute(
                select(
                    Prediction.match_id,
                    func.max(Prediction.league_id),
                    func.max(Prediction.valve_tier),
                )
                .where(Prediction.match_id.in_(match_ids))
                .group_by(Prediction.match_id)
            )
        ).all()
    }

    def league_of(match: Match) -> int | None:
        return match.league_id or predicted_league.get(match.match_id, (None, None))[0]
```

Replace `league_ids = {match.league_id for match in matches.values() if match.league_id}` with:

```python
    league_ids = {league for match in matches.values() if (league := league_of(match))}
```

In the result loop replace
`league_name, tier = leagues.get(match.league_id or 0, (None, None))`
with:

```python
        match_league = league_of(match)
        league_name, liquipedia_tier = leagues.get(match_league or 0, (None, None))
        valve_tier = predicted_league.get(match_id, (None, None))[1]
```

and in `RecentMatch(...)` replace `league_id=match.league_id,` with `league_id=match_league,` and `tier=tier or "unknown",` with `tier=display_tier(valve_tier, liquipedia_tier),`.

- [ ] **Step 4: Implement the route parameter**

In `app/api/routes/matches.py` replace the `recent` route with:

```python
@router.get("/recent", response_model=list[RecentMatch])
async def recent(
    limit: int = Query(default=20, ge=1, le=50),
    tiers: str | None = Query(
        default=None,
        pattern=r"^(tier1|tier2|tier3|unknown)(,(tier1|tier2|tier3|unknown))*$",
        description="Comma-separated display tiers, e.g. tier1,unknown",
    ),
    session: AsyncSession = Depends(get_session),
) -> list[RecentMatch]:
    """Matches we predicted in professional leagues and then saw finish, newest first.

    Declared above `/{match_id}`, or `recent` is swallowed by it as a match id.
    """
    wanted = tiers.split(",") if tiers else None
    return await recent_matches(session, limit=limit, tiers=wanted)
```

- [ ] **Step 5: Run to verify pass**

Run: `docker compose run --rm tools python -m pytest tests/test_recent.py -v`
Expected: PASS (all, none skipped)

- [ ] **Step 6: Commit**

```bash
git add app/api/recent.py app/api/routes/matches.py tests/test_recent.py
git commit -m "limit the played feed to professional leagues and filter it by tier"
```

---

### Task 7: Accuracy dashboard by segment

**Files:**
- Modify: `app/api/accuracy.py`
- Modify: `app/api/routes/model.py:56-79`
- Modify: `app/schemas/common.py:221-254` (`ModelMetrics`; new `SegmentCount`)
- Test: `tests/test_accuracy.py`

**Interfaces:**
- Consumes: `Segment`, `SEGMENTS`, `segment_expr` (Tasks 1-2).
- Produces (Task 8 uses `load_scored`):
  - `load_scored(session: AsyncSession, version: str, segment: Segment | None = None) -> list[ScoredPrediction]`
  - `serving_progress(session: AsyncSession, version: str, segment: Segment | None = None) -> ServingProgress`
  - `segment_counts(session: AsyncSession, version: str) -> tuple[list[SegmentCount], int]` — counts for all three segments (zeros included) and the unsegmented count
  - `class SegmentCount(BaseModel)`: `segment: str`, `matches: int`
  - `ModelMetrics.segment: str = "tier1"`, `ModelMetrics.segments: list[SegmentCount] = []`, `ModelMetrics.unsegmented_matches: int = 0`
  - `GET /api/model/metrics?version=&segment=tier1|pro|excluded` (default `tier1`, 422 otherwise)

- [ ] **Step 1: Write the failing tests**

In `tests/test_accuracy.py`:

Add imports `from fastapi.testclient import TestClient` and `from app.db.models.reference import League`, and add `segment_counts, serving_progress` to the `app.api.accuracy` import (then drop the two local `from app.api.accuracy import serving_progress` lines inside `TestTrainingStatus`).

Replace `add_prediction` with:

```python
async def add_prediction(
    session: AsyncSession,
    match_id: int,
    minute: int,
    p_radiant: float,
    *,
    version: str = VERSION,
    at: datetime | None = None,
    league_id: int | None = None,
    valve_tier: str | None = None,
) -> None:
    session.add(
        Prediction(
            match_id=match_id,
            minute=minute,
            predicted_at=at or BASE + timedelta(minutes=minute),
            model_version=version,
            p_radiant=p_radiant,
            features={},
            league_id=league_id,
            valve_tier=valve_tier,
        )
    )
    await session.flush()
```

Append:

```python
async def seed_segments(session: AsyncSession) -> None:
    """One scored match per segment, plus one whose Valve tier was never known."""
    session.add_all([League(league_id=1, tier="tier1"), League(league_id=2)])
    await session.flush()
    for match_id in (1, 2, 3, 4):
        await add_match(session, match_id, radiant_win=True)
    await add_prediction(session, 1, 10, 0.7, league_id=1, valve_tier="professional")
    await add_prediction(session, 2, 10, 0.6, league_id=2, valve_tier="professional")
    await add_prediction(session, 3, 10, 0.4, league_id=3, valve_tier="excluded")
    await add_prediction(session, 4, 10, 0.5, league_id=None, valve_tier=None)


class TestSegments:
    """Measured 11.09.2026: every scored prediction on production came from an amateur league.
    A dashboard that pools them describes a domain the model never trained on."""

    async def test_each_segment_scores_only_its_own_predictions(
        self, session: AsyncSession
    ) -> None:
        await seed_segments(session)

        assert [r.match_id for r in await load_scored(session, VERSION, "tier1")] == [1]
        assert [r.match_id for r in await load_scored(session, VERSION, "pro")] == [2]
        assert [r.match_id for r in await load_scored(session, VERSION, "excluded")] == [3]
        # No segment means everything, as before - the drift check's old callers rely on it.
        assert len(await load_scored(session, VERSION)) == 4

    async def test_counts_list_every_segment_and_the_unsegmented(
        self, session: AsyncSession
    ) -> None:
        await seed_segments(session)

        counts, unsegmented = await segment_counts(session, VERSION)

        assert [(c.segment, c.matches) for c in counts] == [
            ("tier1", 1),
            ("pro", 1),
            ("excluded", 1),
        ]
        assert unsegmented == 1

    async def test_an_empty_version_still_lists_all_three(self, session: AsyncSession) -> None:
        counts, unsegmented = await segment_counts(session, "never-served")
        assert [(c.segment, c.matches) for c in counts] == [
            ("tier1", 0),
            ("pro", 0),
            ("excluded", 0),
        ]
        assert unsegmented == 0

    async def test_progress_is_counted_inside_the_segment(self, session: AsyncSession) -> None:
        await seed_segments(session)
        session.add(Match(match_id=5))
        await session.flush()
        await add_prediction(session, 5, 3, 0.5, league_id=3, valve_tier="excluded")

        progress = await serving_progress(session, VERSION, "excluded")

        assert progress.predicted_matches == 2

    def test_the_route_refuses_an_unknown_segment(self, client: TestClient) -> None:
        response = client.get("/api/model/metrics", params={"segment": "everything"})
        assert response.status_code == 422
```

- [ ] **Step 2: Run to verify failure**

Run: `docker compose run --rm tools python -m pytest tests/test_accuracy.py -v`
Expected: FAIL — `ImportError: cannot import name 'segment_counts'`

- [ ] **Step 3: Add the schema**

In `app/schemas/common.py`, directly above `class ModelMetrics`, add:

```python
class SegmentCount(BaseModel):
    """Scored matches of one version in one segment (design 2026-09-11-pro-segment)."""

    segment: str
    matches: int
```

In `ModelMetrics`, after `versions: list[ModelVersionInfo] = []` add:

```python
    #: Which slice the numbers above describe: tier1, pro or excluded. Everything above is
    #: computed inside it, including `predicted_matches` and `awaiting_outcome`.
    segment: str = "tier1"
    #: Scored matches of this version per segment - all three, zeros included - so the page can
    #: say where the data is when the selected slice has none.
    segments: list[SegmentCount] = []
    #: Scored matches whose league's Valve tier was unknown when predicted: in no segment.
    unsegmented_matches: int = 0
```

- [ ] **Step 4: Implement in `app/api/accuracy.py`**

Add imports:

```python
from app.db.models.reference import League
from app.domain.segments import SEGMENTS, Segment, segment_expr
```

and add `SegmentCount` to the `app.schemas.common` import.

Append to the module docstring:

```
**Segments are never mixed either.** The live poller predicts every league Valve reports,
amateur cups included, and those stay in the log as a control group. Every query here can be
narrowed to one segment - Tier 1, Pro, Excluded - read from the Valve tier the prediction
recorded when it was served and the Liquipedia tier its league has now.
```

Add a helper below `ScoredPrediction`:

```python
def _in_segment(statement: Select[Any], segment: Segment) -> Select[Any]:
    """Narrow a statement that selects from `predictions` to one segment."""
    return statement.outerjoin(League, League.league_id == Prediction.league_id).where(
        segment_expr(Prediction.valve_tier, League.tier) == segment
    )
```

Change `_scored_rows` signature to `def _scored_rows(version: str | None = None, segment: Segment | None = None) -> Select[Any]:` and before `return statement` add:

```python
    if segment is not None:
        statement = _in_segment(statement, segment)
```

Replace `load_scored` with:

```python
async def load_scored(
    session: AsyncSession, version: str, segment: Segment | None = None
) -> list[ScoredPrediction]:
    rows = (await session.execute(_scored_rows(version, segment))).all()
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
```

Replace the body of `serving_progress` (keep the docstring) and its signature with:

```python
async def serving_progress(
    session: AsyncSession, version: str, segment: Segment | None = None
) -> ServingProgress:
    statement = (
        select(
            func.count(func.distinct(Prediction.match_id)),
            func.min(Prediction.predicted_at),
            func.max(Prediction.predicted_at),
        )
        .select_from(Prediction)
        .where(Prediction.model_version == version)
    )
    if segment is not None:
        statement = _in_segment(statement, segment)
    row = (await session.execute(statement)).one()
    return ServingProgress(
        predicted_matches=int(row[0] or 0),
        first_prediction_at=row[1],
        last_prediction_at=row[2],
    )
```

(Place the existing docstring as the first statement of the new function body.)

Add after `serving_progress`:

```python
async def segment_counts(session: AsyncSession, version: str) -> tuple[list[SegmentCount], int]:
    """Scored matches of one version per segment, and how many fall in none.

    All three segments are always listed: "Tier 1: 0" is the answer the page exists to give
    on most days, and a missing key would make it look like an error.
    """
    # Grouped through a subquery, not by the CASE itself: its IN-lists are bound parameters,
    # numbered differently in SELECT and GROUP BY, and Postgres then rejects the pair as two
    # different expressions.
    inner = (
        select(
            Prediction.match_id,
            segment_expr(Prediction.valve_tier, League.tier).label("segment"),
        )
        .select_from(Prediction)
        .join(Match, Match.match_id == Prediction.match_id)
        .outerjoin(League, League.league_id == Prediction.league_id)
        .where(Match.radiant_win.is_not(None), Prediction.model_version == version)
        .subquery()
    )
    rows = (
        await session.execute(
            select(inner.c.segment, func.count(func.distinct(inner.c.match_id))).group_by(
                inner.c.segment
            )
        )
    ).all()
    found: dict[str | None, int] = {row[0]: int(row[1]) for row in rows}
    counts = [SegmentCount(segment=name, matches=found.get(name, 0)) for name in SEGMENTS]
    return counts, found.get(None, 0)
```

- [ ] **Step 5: Implement the route**

In `app/api/routes/model.py` change the accuracy import to
`from app.api.accuracy import load_scored, metrics_from, scored_versions, segment_counts, serving_progress`,
add `from app.domain.segments import Segment`, and replace `model_metrics` with:

```python
@router.get("/metrics", response_model=ModelMetrics)
async def model_metrics(
    version: str | None = None,
    segment: Segment = "tier1",
    session: AsyncSession = Depends(get_session),
) -> ModelMetrics:
    """Calibration of served predictions, scored against how the matches actually ended.

    Defaults to the version currently being served rather than to whichever has the most
    data. When that version has nothing scored yet the page shows an empty dashboard, and
    that is the honest answer: an older model's calibration says nothing about the numbers
    a visitor is looking at right now. The other versions are listed so they stay reachable.

    Defaults to the Tier 1 segment for the same reason: it is the domain the product promises,
    and on most days it is empty. `segments` says where the data is instead.
    """
    versions = await scored_versions(session)
    wanted = version or get_predictor().version
    metrics = metrics_from(wanted, await load_scored(session, wanted, segment))
    metrics.versions = versions
    metrics.segment = segment
    metrics.segments, metrics.unsegmented_matches = await segment_counts(session, wanted)

    progress = await serving_progress(session, wanted, segment)
    metrics.predicted_matches = progress.predicted_matches
    metrics.awaiting_outcome = max(progress.predicted_matches - metrics.matches, 0)
    metrics.first_prediction_at = progress.first_prediction_at
    metrics.last_prediction_at = progress.last_prediction_at
    metrics.training = _training(wanted)
    return metrics
```

- [ ] **Step 6: Run to verify pass**

Run: `docker compose run --rm tools python -m pytest tests/test_accuracy.py -v`
Expected: PASS (all, none skipped)

- [ ] **Step 7: Commit**

```bash
git add app/api/accuracy.py app/api/routes/model.py app/schemas/common.py tests/test_accuracy.py
git commit -m "compute the accuracy dashboard per segment, Tier 1 by default"
```

---

### Task 8: Drift alert per segment

**Files:**
- Modify: `app/workers/drift.py`
- Test: `tests/workers/test_drift_worker.py` (create)

**Interfaces:**
- Consumes: `load_scored(session, version, segment)`, `scored_versions` (Task 7), `SEGMENTS`, `Segment` (Task 1), `check_drift` (existing).
- Produces: `drift_verdicts(session_factory: async_sessionmaker[AsyncSession], now: datetime) -> list[tuple[Segment, DriftVerdict]]`

- [ ] **Step 1: Write the failing test**

Create `tests/workers/test_drift_worker.py`:

```python
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
```

- [ ] **Step 2: Run to verify failure**

Run: `docker compose run --rm tools python -m pytest tests/workers/test_drift_worker.py -v`
Expected: FAIL — `ImportError: cannot import name 'drift_verdicts'`

- [ ] **Step 3: Implement**

Replace everything in `app/workers/drift.py` below the module docstring with:

```python
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
```

In the module docstring, after the paragraph starting `Every version with scored predictions`, add:

```
Every version is checked in each segment separately - Tier 1, Pro, Excluded - so a shift in
which leagues were on air does not read as the model drifting.
```

- [ ] **Step 4: Run to verify pass**

Run: `docker compose run --rm tools python -m pytest tests/workers/test_drift_worker.py tests/ml/test_drift.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/workers/drift.py tests/workers/test_drift_worker.py
git commit -m "check calibration drift per segment"
```

---

### Task 9: `backfill-segments` for predictions logged before the columns existed

**Files:**
- Create: `app/ingestion/segments_backfill.py`
- Modify: `app/ingestion/cli.py` (command function, parser, dispatch)
- Test: `tests/ingestion/test_segments_backfill.py` (create)

**Interfaces:**
- Consumes: `Prediction.league_id/valve_tier`, `League.valve_tier` (Task 2), `RawLiveSnapshot` (existing).
- Produces: `backfill_segments(session_factory: async_sessionmaker[AsyncSession]) -> SegmentBackfillReport` with `league_ids: int`, `valve_tiers: int`; CLI `python -m app.ingestion.cli backfill-segments`.

- [ ] **Step 1: Write the failing test**

Create `tests/ingestion/test_segments_backfill.py`:

```python
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
```

- [ ] **Step 2: Run to verify failure**

Run: `docker compose run --rm tools python -m pytest tests/ingestion/test_segments_backfill.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.ingestion.segments_backfill'`

- [ ] **Step 3: Implement**

Create `app/ingestion/segments_backfill.py`:

```python
"""One-off fill of `predictions.league_id` and `predictions.valve_tier` (design 2026-09-11).

Both columns appeared after the live loop had been logging for days. The league is exact: the
poller's own snapshot of the match names it. The Valve tier is not point-in-time - it is the
league's tier *now* - which is acceptable for rows a few days old and would not be for an
archive: Valve re-tags leagues over months (34% of the summary archive is in leagues that are
`excluded` today). Run after `reference`, which is what loads the tiers.

Only NULLs are filled. A value already present was written at serve time and is better
knowledge than anything this pass has.
"""

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

LEAGUE_FROM_SNAPSHOTS = text(
    """
    UPDATE predictions AS p
    SET league_id = s.league_id
    FROM (
        SELECT DISTINCT ON (match_id)
            match_id,
            NULLIF(payload->>'league_id', '0')::bigint AS league_id
        FROM raw_live_snapshots
        WHERE payload->>'league_id' IS NOT NULL
        ORDER BY match_id, captured_at DESC
    ) AS s
    WHERE p.match_id = s.match_id
      AND p.league_id IS NULL
      AND s.league_id IS NOT NULL
    """
)

TIER_FROM_LEAGUES = text(
    """
    UPDATE predictions AS p
    SET valve_tier = l.valve_tier
    FROM leagues AS l
    WHERE p.league_id = l.league_id
      AND p.valve_tier IS NULL
      AND l.valve_tier IS NOT NULL
    """
)


@dataclass(frozen=True)
class SegmentBackfillReport:
    league_ids: int
    valve_tiers: int


async def backfill_segments(
    session_factory: async_sessionmaker[AsyncSession],
) -> SegmentBackfillReport:
    async with session_factory() as session:
        leagues = await session.execute(LEAGUE_FROM_SNAPSHOTS)
        tiers = await session.execute(TIER_FROM_LEAGUES)
        await session.commit()
    return SegmentBackfillReport(
        # CursorResult carries rowcount; the base Result type mypy infers here does not.
        league_ids=int(leagues.rowcount or 0),  # type: ignore[attr-defined]
        valve_tiers=int(tiers.rowcount or 0),  # type: ignore[attr-defined]
    )
```

- [ ] **Step 4: Wire the CLI**

In `app/ingestion/cli.py`:

- add `from app.ingestion.segments_backfill import backfill_segments` to the imports (isort order);
- after `cmd_reference` add:

```python
async def cmd_backfill_segments() -> None:
    """Fill league and Valve tier on predictions logged before they were recorded.

    Run after `reference`. Idempotent: only NULLs are filled.
    """
    report = await backfill_segments(get_session_factory())
    print(f"predictions given a league:     {report.league_ids}")
    print(f"predictions given a Valve tier: {report.valve_tiers}")
    print("  (the league's tier now, not at prediction time - fine for rows days old)")
```

- next to the `reference` parser add:

```python
    sub.add_parser(
        "backfill-segments",
        help="fill league and Valve tier on predictions logged before they were recorded",
    )
```

- in the dispatch chain, after the `reference` branch add:

```python
            elif args.command == "backfill-segments":
                await cmd_backfill_segments()
```

- [ ] **Step 5: Run to verify pass**

Run: `docker compose run --rm tools python -m pytest tests/ingestion/test_segments_backfill.py -v`
Expected: PASS (5 passed, none skipped)

Run: `docker compose run --rm tools python -m app.ingestion.cli --help`
Expected: `backfill-segments` is listed.

- [ ] **Step 6: Commit**

```bash
git add app/ingestion/segments_backfill.py app/ingestion/cli.py tests/ingestion/test_segments_backfill.py
git commit -m "add backfill-segments for predictions logged before the segment columns"
```

---

### Task 10: Documentation, full check, push, merge into `development`

**Files:**
- Modify: `CLAUDE.md` (backend)
- Modify: `docs/spec.md` (§3)
- Modify (not in git): `../dota2-prediction-spec.md` (§3), `../CLAUDE.md` («Текущий шаг», «Порядок команд пайплайна»)

- [ ] **Step 1: Update spec §3 in both copies**

In `docs/spec.md` and `../dota2-prediction-spec.md` replace the line

```
Fallback до готовности маппинга — `tier == "premium"` из `/leagues` OpenDota.
```

with:

```
Fallback до готовности маппинга — `tier == "premium"` из `/leagues` OpenDota. Замер 11.09.2026
сузил его смысл: `premium` у Valve — практически только The International, а все прочие Tier 1
(DreamLeague, BLAST Slam, ESL One, PGL Wallachia, EWC) — `professional`, наравне с Tier 3.
Поэтому тир Valve служит **фильтром «про / не про»** для live-ленты и сегментов точности
(`professional`/`premium` против `excluded`/`amateur`), а Tier 1 выделяет только разметка
Liquipedia. Тир Valve — текущий, не исторический: 34% сводок архива лежат в лигах, которые
сейчас `excluded`, поэтому на прогноз он записывается в момент выдачи.
```

The frontend copy (`dota-oracle-frontend/docs/spec.md`) is updated by the frontend plan.

- [ ] **Step 2: Update backend `CLAUDE.md`**

In the «Текущее состояние» list, directly before the paragraph that starts `Критерий выхода из фазы 1 (§11)`, add:

```markdown
- **Лента и точность разделены на сегменты (12.09.2026, дизайн `2026-09-11-pro-segment`).**
  Замер на проде 11.09: все 249 сверенных live-прогнозов были из лиг, которые Valve
  помечает `excluded` (AD2L, FACEIT, миксер-кап турниры), — дашборд точности описывал домен,
  которого модель не видела. Теперь:
  - `leagues.valve_tier` — тир Valve из `/leagues` (`premium`/`professional`/`amateur`/
    `excluded`), отдельно от `tier` Liquipedia. Обновляет `refresh_valve_tiers`: cron в :03 и
    по запросу поллера, не чаще раза в 15 минут (ключ `valve_tiers:throttle`).
  - Поллер пишет на прогноз `league_id` и `valve_tier` **в момент выдачи** и продолжает
    предсказывать все лиги — любительские остаются контрольной группой.
  - Правило сегмента одно — `app/domain/segments.py`, в Python и SQL, паритет держит тест:
    Valve-pro + Liquipedia `tier1` (или `premium` без разметки) → `tier1`; прочие
    `professional`/`premium` → `pro`; `excluded`/`amateur` → `excluded`; тир неизвестен →
    вне сегментов.
  - `/api/matches/live` и `/api/matches/recent` отдают только про-лиги; `/recent` фильтрует
    по `tiers`. `/api/model/metrics` считает внутри `segment` (по умолчанию `tier1`), дрейф —
    на каждую пару (версия × сегмент).
  - **`premium` у Valve — практически только TI**; остальной Tier 1 — `professional`, как и
    Tier 3. **Тир Valve — текущий**: 34% сводок архива в лигах, ставших `excluded`. Отсюда
    запись на прогноз, а не джойн.
  - **`sync_liquipedia` на кроне — заглушка** (`TODO(phase-2)`): новая лига получает тир
    Liquipedia только после ручного `map-leagues`. До этого она видна в ленте под чипом
    «Без разметки», если Valve считает её про.
  - После выката на прод: `reference`, затем `backfill-segments` (заполняет NULL у старых
    прогнозов; тир — текущий, не на момент прогноза).
```

In the «Команды» section, after the line `docker compose run --rm tools python -m app.ingestion.cli reference   # герои и имена игроков`, add:

```bash
docker compose run --rm tools python -m app.ingestion.cli backfill-segments  # лига и тир Valve у старых прогнозов
```

- [ ] **Step 3: Update the root `../CLAUDE.md` (not in git — no commit)**

In «Порядок команд пайплайна», in the bash block, add right after the `resolve-outcomes` line:

```bash
docker compose run --rm tools python -m app.ingestion.cli reference            # тиры Valve для ленты
docker compose run --rm tools python -m app.ingestion.cli backfill-segments    # разово после выката сегментов
```

In «Текущий шаг», add a short paragraph at the top of the section:

```markdown
### Сегменты: лента без любительских лиг (12.09.2026)

Прод предсказывал и мерил любительские игры: все 249 сверенных прогнозов — из лиг Valve
`excluded`. Лента и «Сыграно» теперь показывают только про-лиги (тир Valve), дашборд и дрейф
считаются по сегментам Tier 1 / Pro / Excluded, любительские остаются контрольной группой.
Дизайн — `dota-oracle-backend/docs/superpowers/specs/2026-09-11-pro-segment-design.md`.
`sync_liquipedia` на кроне — заглушка: разметка новых лиг по-прежнему ручная.
```

- [ ] **Step 4: Run the full CI-equivalent check**

Run: `docker compose run --rm -e STRATZ_API_TOKEN= -e STEAM_API_KEY= tools python -m pytest`
Expected: all pass; the summary shows only the 2 long-standing skips (if more tests are skipped, the DB is not reachable — fix that and rerun).

Run: `docker compose run --rm tools sh -c "ruff check . && ruff format --check . && mypy app"`
Expected: `All checks passed!`, no files would be reformatted, `Success: no issues found`. If `ruff format --check` fails, run `docker compose run --rm tools ruff format .`, review the diff, and include it in the commit below.

- [ ] **Step 5: Commit the docs**

```bash
git add CLAUDE.md docs/spec.md
git commit -m "document the pro segment and the Valve tier measurements"
```

- [ ] **Step 6: Push and merge into `development`**

```bash
git push -u origin feature/pro-segment
git switch development && git pull --ff-only
git merge --no-ff feature/pro-segment -m "merge feature/pro-segment into development"
git push origin development
git branch -d feature/pro-segment
git push origin --delete feature/pro-segment
```

Stop here. Merging `development` into `main` (production) requires the owner's explicit permission for that specific deploy, and is done together with the frontend image (`./deploy.sh <api-sha> <spa-sha>`), followed on the server by:

```bash
docker compose -f docker-compose.prod.yml run --rm api python -m app.ingestion.cli reference
docker compose -f docker-compose.prod.yml run --rm api python -m app.ingestion.cli backfill-segments
```

If the auth / pipeline-panel work (`docs/superpowers/specs/2026-09-11-auth-and-pipeline-panel-design.md`) has already landed in `development` by now, add `backfill-segments` (no parameters, no destructive flags) to its command registry in the same merge, so its CLI-parity test keeps passing.
