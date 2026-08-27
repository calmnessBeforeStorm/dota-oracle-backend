# STRATZ Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Сделать STRATZ единственным источником поминутных рядов, из которых строятся `match_snapshots`, и тем самым снять суточную квоту OpenDota как блокер фазы 4.

**Architecture:** Новый адаптер `app/features/adapters/stratz.py` приводит payload `match(id:)` к тому же `GameState`, что и существующие адаптеры OpenDota и Steam — единая точка расчёта признаков (`app/features/live.py`) не меняется. Декодер состояния строений остаётся общим: `buildings.py` получает разбор `npcId` рядом с разбором имён из OpenDota и битмасок из Steam. Воркер деталей и `normalize` параметризуются источником; `featurize` переключается на `RawSource.STRATZ_MATCH`.

**Tech Stack:** Python 3.12, httpx, SQLAlchemy 2 async, asyncpg, pytest, ruff, mypy (strict), Docker Compose.

**Spec:** [docs/superpowers/specs/2026-08-27-stratz-adapter-design.md](../specs/2026-08-27-stratz-adapter-design.md)

## Global Constraints

- Ветка `feature/stratz-adapter`, ветвится от `development`. **В `main` не коммитить и не пушить.**
- Коммиты подписываются `ersaim.adilet@yandex.kz`. Трейлеров об авторстве ИИ нет.
- Сообщения коммитов и комментарии в коде — **на английском**, в императиве. Документация — русская.
- Один коммит — одно логическое изменение.
- Перед пушем: `ruff check . && ruff format --check . && mypy app && pytest`. `mypy` в режиме `strict`.
- Тесты гонять в контейнере: `docker compose exec api python -m pytest` (сейчас 322 теста).
- Инвариант: никакой информации из будущего в признаках. Всё point-in-time.
- Инвариант: признаки считаются только в `app/features/live.py`, из `GameState`.
- Инвариант: `/matches/{id}` и `match(id:)` **не владеют принадлежностью к серии** — её владеет слой сводок `/proMatches`, разбор деталей не имеет права её перезаписывать.
- Инвариант: неизвестное хранится как `NULL`, а не как значение по умолчанию.
- Выравнивание рядов STRATZ (измерено, не выведено): `radiantNetworthLeads[minute + 1]`, `radiantExperienceLeads[minute + 1]`, `networthPerMinute[minute]`, килы — `sum(radiantKills[:minute + 1])`.

---

### Task 1: Запрос матча в STRATZ-клиенте и фикстуры

**Files:**
- Modify: `app/ingestion/clients/stratz.py`
- Create: `tests/ingestion/test_stratz_client.py`
- Create: `tests/fixtures/stratz/match_8944612322.json`, `match_8946228107.json`, `match_8946228708.json`

**Interfaces:**
- Consumes: `BaseClient.post_json`, `StratzClient.query` (уже есть)
- Produces: `MATCH_QUERY: str`, `StratzClient.match(match_id: int) -> dict[str, Any]`

Три id выбраны намеренно: под ними уже лежат фикстуры OpenDota в `tests/fixtures/opendota/`, так что паритетные тесты получают пары payload-ов одного и того же матча. Наличие и разобранность всех трёх в STRATZ проверены.

- [ ] **Step 1: Написать падающий тест клиента**

```python
# tests/ingestion/test_stratz_client.py
from typing import Any

import pytest

from app.ingestion.clients.stratz import MATCH_QUERY, StratzClient


class FakeQuery:
    """Records what was asked for and returns a canned match."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, query: str, **variables: Any) -> dict[str, Any]:
        self.calls.append((query, variables))
        return {"match": {"id": variables["id"], "durationSeconds": 1800}}


async def test_match_asks_for_the_match_query(monkeypatch: pytest.MonkeyPatch) -> None:
    client = StratzClient()
    fake = FakeQuery()
    monkeypatch.setattr(client, "query", fake)

    payload = await client.match(8944612322)

    assert payload["id"] == 8944612322
    query, variables = fake.calls[0]
    assert query is MATCH_QUERY
    assert variables == {"id": 8944612322}
    await client.aclose()


async def test_match_query_asks_for_every_field_the_adapter_needs() -> None:
    """The adapter is built on exactly these fields; dropping one from the query would
    fail at snapshot time rather than here, and only for some matches."""
    for field in (
        "radiantNetworthLeads",
        "radiantExperienceLeads",
        "radiantKills",
        "direKills",
        "networthPerMinute",
        "towerDeaths",
        "didRadiantWin",
        "durationSeconds",
        "parsedDateTime",
    ):
        assert field in MATCH_QUERY
```

- [ ] **Step 2: Прогнать тест, убедиться, что падает**

Run: `docker compose exec api python -m pytest tests/ingestion/test_stratz_client.py -v`
Expected: FAIL — `ImportError: cannot import name 'MATCH_QUERY'`

- [ ] **Step 3: Добавить запрос и метод**

```python
# app/ingestion/clients/stratz.py
MATCH_QUERY = """
query Match($id: Long!) {
  match(id: $id) {
    id didRadiantWin durationSeconds startDateTime endDateTime parsedDateTime
    leagueId radiantTeamId direTeamId gameVersionId
    radiantNetworthLeads radiantExperienceLeads radiantKills direKills
    pickBans { isPick heroId bannedHeroId isRadiant order }
    players {
      steamAccountId heroId isRadiant playerSlot
      kills deaths assists numLastHits numDenies
      networth goldPerMinute experiencePerMinute leaverStatus lane
      stats { networthPerMinute }
    }
    towerDeaths { time npcId isRadiant }
  }
}
"""
```

```python
    async def match(self, match_id: int) -> dict[str, Any]:
        """One call per map. The per-minute series live here, not in the league listing.

        `playbackData` is deliberately absent from the query: on our token it comes back
        with every list empty, so asking for it costs response size and returns nothing.
        """
        data = await self.query(MATCH_QUERY, id=match_id)
        return dict(data.get("match") or {})
```

- [ ] **Step 4: Прогнать тест, убедиться, что проходит**

Run: `docker compose exec api python -m pytest tests/ingestion/test_stratz_client.py -v`
Expected: PASS (2 теста)

- [ ] **Step 5: Скачать фикстуры**

```bash
docker compose exec -T api python - <<'PY'
import asyncio, json, pathlib
from app.ingestion.clients.stratz import StratzClient
OUT = pathlib.Path("/app/tests/fixtures/stratz"); OUT.mkdir(parents=True, exist_ok=True)
async def main():
    async with StratzClient() as c:
        for i in (8944612322, 8946228107, 8946228708):
            m = await c.match(i)
            assert m and m.get("parsedDateTime"), i
            (OUT / f"match_{i}.json").write_text(json.dumps(m, indent=1), encoding="utf-8")
            print(i, "ok", len(m["radiantNetworthLeads"]), "minutes")
asyncio.run(main())
PY
```

Expected: три строки `ok`, три файла в `tests/fixtures/stratz/`.

- [ ] **Step 6: Коммит**

```bash
git add app/ingestion/clients/stratz.py tests/ingestion/test_stratz_client.py tests/fixtures/stratz
git commit -m "add STRATZ single-match query and fixtures"
```

---

### Task 2: Разбор `npcId` в общем декодере строений

**Files:**
- Modify: `app/features/buildings.py`
- Test: `tests/features/test_buildings.py`

**Interfaces:**
- Consumes: `BuildingKill`, `BuildingState`, `apply_kill`, `full_base`, `LANES`, `BASE` (всё уже есть)
- Produces: `NPC_BUILDINGS: dict[int, BuildingKill]`, `parse_npc_id(npc_id: int) -> BuildingKill | None`, `state_at_npc(deaths: list[dict[str, Any]], minute: int, radiant: bool) -> BuildingState`

Таблица снята сопоставлением с `objectives` OpenDota и проверена на 8 матчах: после фильтрации по ней число событий совпадает с числом `building_kill` точно. `36` и `37` соответствия у OpenDota не имеют вовсе и отбрасываются.

- [ ] **Step 1: Написать падающие тесты**

```python
# добавить в tests/features/test_buildings.py
from app.features.buildings import BASE, BuildingKill, parse_npc_id, state_at_npc


class TestParseNpcId:
    def test_radiant_tier_one_towers(self) -> None:
        for npc_id, lane in ((16, "top"), (17, "mid"), (18, "bot")):
            kill = parse_npc_id(npc_id)
            assert kill is not None
            assert (kill.is_radiant, kill.kind, kill.lane) == (True, "tower", lane)

    def test_dire_tier_three_towers(self) -> None:
        for npc_id, lane in ((32, "top"), (33, "mid"), (34, "bot")):
            kill = parse_npc_id(npc_id)
            assert kill is not None
            assert (kill.is_radiant, kill.kind, kill.lane) == (False, "tower", lane)

    def test_ancient_towers_have_no_lane(self) -> None:
        assert parse_npc_id(25) == BuildingKill(True, "tower", BASE)
        assert parse_npc_id(35) == BuildingKill(False, "tower", BASE)

    def test_barracks(self) -> None:
        assert parse_npc_id(38) == BuildingKill(True, "barracks", "top")
        assert parse_npc_id(43) == BuildingKill(True, "barracks", "bot")
        assert parse_npc_id(44) == BuildingKill(False, "barracks", "top")
        assert parse_npc_id(49) == BuildingKill(False, "barracks", "bot")

    def test_forts(self) -> None:
        assert parse_npc_id(50) == BuildingKill(True, "ancient", BASE)
        assert parse_npc_id(51) == BuildingKill(False, "ancient", BASE)

    def test_unmapped_ids_are_ignored(self) -> None:
        """36 and 37 fire several times a match and have no counterpart in OpenDota's
        objectives log at all. Counting them would destroy buildings that still stand."""
        assert parse_npc_id(36) is None
        assert parse_npc_id(37) is None
        assert parse_npc_id(0) is None
        assert parse_npc_id(999) is None


class TestStateAtNpc:
    DEATHS = [
        {"time": 400, "npcId": 16, "isRadiant": True},
        {"time": 800, "npcId": 19, "isRadiant": True},
        {"time": 900, "npcId": 36, "isRadiant": True},
        {"time": 1000, "npcId": 26, "isRadiant": False},
    ]

    def test_only_events_up_to_the_minute_are_applied(self) -> None:
        assert state_at_npc(self.DEATHS, minute=5, radiant=True).towers["top"] == 3
        assert state_at_npc(self.DEATHS, minute=6, radiant=True).towers["top"] == 2
        assert state_at_npc(self.DEATHS, minute=13, radiant=True).towers["top"] == 1

    def test_the_other_side_is_untouched(self) -> None:
        assert state_at_npc(self.DEATHS, minute=20, radiant=False).towers["top"] == 2
        assert state_at_npc(self.DEATHS, minute=20, radiant=False).tower_count == 10

    def test_unmapped_events_change_nothing(self) -> None:
        without = [e for e in self.DEATHS if e["npcId"] != 36]
        assert state_at_npc(self.DEATHS, 20, True) == state_at_npc(without, 20, True)
```

- [ ] **Step 2: Прогнать, убедиться, что падают**

Run: `docker compose exec api python -m pytest tests/features/test_buildings.py -v`
Expected: FAIL — `ImportError: cannot import name 'parse_npc_id'`

- [ ] **Step 3: Реализовать**

```python
# app/features/buildings.py, ниже parse_building_key

def _towers(first: int, is_radiant: bool) -> dict[int, BuildingKill]:
    """Nine lane towers, numbered tier-major: t1 top/mid/bot, then t2, then t3."""
    return {
        first + tier * 3 + index: BuildingKill(is_radiant, "tower", lane)
        for tier in range(3)
        for index, lane in enumerate(LANES)
    }


def _barracks(first: int, is_radiant: bool) -> dict[int, BuildingKill]:
    """Six racks: melee top/mid/bot, then ranged top/mid/bot."""
    return {
        first + kind * 3 + index: BuildingKill(is_radiant, "barracks", lane)
        for kind in range(2)
        for index, lane in enumerate(LANES)
    }


#: STRATZ names buildings by `npcId` in `towerDeaths` rather than by the npc name OpenDota
#: puts in its objectives log. The table was read off the two sources side by side on real
#: matches; filtering `towerDeaths` through it reproduces OpenDota's `building_kill` count
#: exactly. Ids 36 and 37 fire repeatedly late in a game and have no counterpart in the
#: objectives log at all - whatever they are, they are not a building we track, and an
#: unknown id must leave the state alone rather than destroy something that still stands.
NPC_BUILDINGS: dict[int, BuildingKill] = {
    **_towers(16, True),
    25: BuildingKill(True, "tower", BASE),
    **_towers(26, False),
    35: BuildingKill(False, "tower", BASE),
    **_barracks(38, True),
    **_barracks(44, False),
    50: BuildingKill(True, "ancient", BASE),
    51: BuildingKill(False, "ancient", BASE),
}


def parse_npc_id(npc_id: int) -> BuildingKill | None:
    """Read a STRATZ `towerDeaths[].npcId`. None for anything not in the table."""
    return NPC_BUILDINGS.get(int(npc_id))


def state_at_npc(
    deaths: list[dict[str, Any]], minute: int, radiant: bool
) -> BuildingState:
    """Building state of one side at the end of the given minute, from STRATZ events.

    Same contract as `state_at`: only events at or before that minute are applied. The
    side comes from the npc id rather than from the event's `isRadiant` field, so a
    disagreement between the two cannot silently corrupt the state.
    """
    state = full_base()
    cutoff = (minute + 1) * 60

    for event in deaths:
        if int(event.get("time", 0)) >= cutoff:
            continue
        kill = parse_npc_id(event.get("npcId") or 0)
        if kill is not None and kill.is_radiant is radiant:
            state = apply_kill(state, kill)

    return state
```

- [ ] **Step 4: Прогнать, убедиться, что проходят**

Run: `docker compose exec api python -m pytest tests/features/test_buildings.py -v`
Expected: PASS

- [ ] **Step 5: Коммит**

```bash
git add app/features/buildings.py tests/features/test_buildings.py
git commit -m "decode STRATZ building npc ids in the shared building decoder"
```

---

### Task 3: Убрать рошановские признаки из вектора

**Files:**
- Modify: `app/features/live.py:36-42`, `app/features/live.py:103-113`
- Modify: `tests/features/test_leakage.py:90-113`
- Modify: `docs/spec.md:396-407`, `../dota-oracle-frontend/docs/spec.md` (та же секция), `dota2-prediction-spec.md` в корне рабочей директории
- Modify: `CLAUDE.md:49`

**Interfaces:**
- Produces: `FEATURE_ORDER` длиной 29 вместо 32. Поля `GameState.roshan_kills`, `.aegis_holder_is_radiant`, `.roshan_respawn_in` **остаются** — они сырьё, а не признаки, и OpenDota-адаптер продолжает их заполнять.

Обоснование целиком в спеке. Коротко: `from_live_league_game` передаёт в `GameState` только `roshan_respawn_in`, а `roshan_kills` и `aegis_holder_is_radiant` не передаёт вовсе — они уже сейчас константы на проде. Третий признак live-табло отдаёт, но STRATZ — нет, и держать ради него вызов OpenDota на каждой карте значит не снять блокер.

- [ ] **Step 1: Написать падающий тест**

```python
# добавить в tests/features/test_live_features.py
def test_roshan_features_are_gone() -> None:
    """They were dropped for the same reason xp_adv was: the serve path cannot supply
    them. `from_live_league_game` never passes roshan_kills or aegis_holder_is_radiant,
    so both were constants in production while training saw real values."""
    for name in ("roshan_kills", "aegis_holder", "roshan_respawn_in"):
        assert name not in FEATURE_ORDER

    state = GameState(
        match_id=1,
        minute=10,
        radiant=TeamState(),
        dire=TeamState(),
        gold_adv=0,
        xp_adv=0,
        roshan_kills=3,
        aegis_holder_is_radiant=True,
        roshan_respawn_in=120,
    )
    assert set(build_live_features(state)) == set(FEATURE_ORDER)


def test_feature_vector_is_29_long() -> None:
    assert len(FEATURE_ORDER) == 29
    assert len(set(FEATURE_ORDER)) == 29
```

- [ ] **Step 2: Прогнать, убедиться, что падает**

Run: `docker compose exec api python -m pytest tests/features/test_live_features.py -v`
Expected: FAIL — `assert 'roshan_kills' not in FEATURE_ORDER`

- [ ] **Step 3: Убрать из `FEATURE_ORDER` и билдера**

В `app/features/live.py` удалить из `FEATURE_ORDER` блок

```python
    # roshan
    "roshan_kills",
    "aegis_holder",
    "roshan_respawn_in",
```

и из `build_live_features` — ключи `"roshan_kills"`, `"aegis_holder"` (вместе с комментарием о трёх состояниях) и `"roshan_respawn_in"`.

В шапке модуля, следом за абзацем про `xp_adv`, добавить второй прецедент:

```
Roshan went the same way, and for a sharper reason: `from_live_league_game` never passes
`roshan_kills` or `aegis_holder_is_radiant` into the GameState at all, so both were
constants in production while training saw real values. `roshan_respawn_in` the live
scoreboard does supply - but STRATZ, now the only source of per-minute training data,
carries no Roshan events whatsoever, so the offline side cannot. All three stay on
GameState as raw data and out of the feature vector.
```

- [ ] **Step 4: Убрать осиротевший тест аегиса**

В `tests/features/test_leakage.py` удалить тест, который проверяет отображение
`aegis_holder` в `1.0 / 0.0 / -1.0` (он обращается к признаку, которого больше нет).
Тесты в `tests/features/test_building_parity.py`, проверяющие `GameState.roshan_kills` и
`.aegis_holder_is_radiant`, **оставить**: поля никуда не делись.

- [ ] **Step 5: Прогнать весь набор**

Run: `docker compose exec api python -m pytest -q`
Expected: PASS. Падений из-за длины вектора быть не должно — `predictions.features` и
`match_snapshots.features` это JSON, а `BaselinePredictor` читает `gold_adv` и `minute`.

- [ ] **Step 6: Обновить спеку в трёх местах и CLAUDE.md**

В `docs/spec.md` §6.1 удалить строку таблицы

```
| Рошан | число убийств, владелец аегиса, время до респауна |
```

и добавить под таблицей абзац:

```
Рошан в вектор не входит. Причина та же, что у `xp_adv` (§6.4): live-канал
`GetLiveLeagueGames` не передаёт ни числа убийств, ни владельца аегиса — они были
константами на проде, — а STRATZ, единственный источник поминутных рядов для обучения,
не отдаёт рошановских событий вовсе. Поля остаются в `GameState` как сырьё.
```

Ту же правку внести в копию спеки во фронтенд-репозитории и в `dota2-prediction-spec.md`
в корне рабочей директории — копии обязаны совпадать.

В `CLAUDE.md` строку `В векторе снапшота 32 признака` заменить на `29 признаков`.

- [ ] **Step 7: Коммит**

```bash
git add app/features/live.py tests/features/test_live_features.py tests/features/test_leakage.py docs/spec.md CLAUDE.md
git commit -m "drop roshan features: the serve path never supplied them"
```

Копии спеки вне этого репозитория коммитить отдельно, в своём репозитории.

---

### Task 4: Адаптер STRATZ → GameState

**Files:**
- Create: `app/features/adapters/stratz.py`
- Create: `tests/features/test_stratz_adapter.py`

**Interfaces:**
- Consumes: `GameState`, `TeamState`, `SeriesContext` из `app.features.game_state`; `BuildingState`, `state_at_npc` из `app.features.buildings`
- Produces: `is_parsed(match: dict[str, Any]) -> bool`, `snapshot_at(match, minute, series=None, prematch_prior=None, prematch=None) -> GameState`, `iter_snapshots(match, series=None, min_minute=0, prematch=None, prematch_prior=None) -> list[GameState]` — сигнатуры зеркалят `app/features/adapters/opendota.py`, чтобы `featurize` не разбирался, какой адаптер зовёт

- [ ] **Step 1: Написать падающие тесты выравнивания**

```python
# tests/features/test_stratz_adapter.py
import json
from pathlib import Path
from typing import Any

import pytest

from app.features.adapters.stratz import is_parsed, iter_snapshots, snapshot_at

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "stratz"
MATCHES = sorted(FIXTURES.glob("match_*.json"))


@pytest.fixture(params=MATCHES, ids=lambda p: p.stem)
def match(request: pytest.FixtureRequest) -> dict[str, Any]:
    return json.loads(request.param.read_text(encoding="utf-8"))


class TestAlignment:
    """The two families of series are offset differently, which is measured, not derived:
    the leads arrays carry an extra leading element for the time before the horn, the kill
    arrays do not. Getting this wrong shifts the strongest feature by a minute."""

    def test_gold_adv_reads_the_leads_array_one_ahead(self, match: dict[str, Any]) -> None:
        leads = match["radiantNetworthLeads"]
        for minute in (0, 5, 10):
            assert snapshot_at(match, minute).gold_adv == leads[minute + 1]

    def test_xp_adv_reads_the_leads_array_one_ahead(self, match: dict[str, Any]) -> None:
        leads = match["radiantExperienceLeads"]
        for minute in (0, 5, 10):
            assert snapshot_at(match, minute).xp_adv == leads[minute + 1]

    def test_net_worth_reads_the_player_array_in_place(self, match: dict[str, Any]) -> None:
        expected = sum(
            p["stats"]["networthPerMinute"][7] for p in match["players"] if p["isRadiant"]
        )
        assert snapshot_at(match, 7).radiant.net_worth == expected

    def test_score_is_the_cumulative_sum_of_the_kill_array(self, match: dict[str, Any]) -> None:
        for minute in (0, 5, 12):
            assert snapshot_at(match, minute).radiant.score == sum(match["radiantKills"][: minute + 1])
            assert snapshot_at(match, minute).dire.score == sum(match["direKills"][: minute + 1])

    def test_gold_adv_equals_the_net_worth_difference(self, match: dict[str, Any]) -> None:
        """Both come from STRATZ and both are net worth, so unlike the OpenDota path they
        must agree exactly. A mismatch means an offset slipped in."""
        for minute in (0, 5, 10):
            state = snapshot_at(match, minute)
            assert state.gold_adv == state.radiant.net_worth - state.dire.net_worth


class TestSnapshots:
    def test_parsed_matches_are_recognised(self, match: dict[str, Any]) -> None:
        assert is_parsed(match)
        assert not is_parsed({"id": 1, "parsedDateTime": None})
        assert not is_parsed({"id": 1, "parsedDateTime": 123, "radiantNetworthLeads": []})

    def test_one_snapshot_per_minute(self, match: dict[str, Any]) -> None:
        snapshots = iter_snapshots(match)
        assert [s.minute for s in snapshots] == list(range(match["durationSeconds"] // 60 + 1))

    def test_picks_are_five_a_side(self, match: dict[str, Any]) -> None:
        state = snapshot_at(match, 0)
        assert len(state.radiant_picks) == 5
        assert len(state.dire_picks) == 5

    def test_buildings_start_whole_and_never_regrow(self, match: dict[str, Any]) -> None:
        snapshots = iter_snapshots(match)
        assert snapshots[0].radiant.tower_count == 11
        assert snapshots[0].dire.barracks_count == 6
        for earlier, later in zip(snapshots, snapshots[1:], strict=False):
            assert later.radiant.tower_count <= earlier.radiant.tower_count
            assert later.dire.barracks_count <= earlier.dire.barracks_count

    def test_unparsed_match_is_refused(self) -> None:
        with pytest.raises(ValueError, match="not parsed"):
            iter_snapshots({"id": 7, "parsedDateTime": None})
```

- [ ] **Step 2: Прогнать, убедиться, что падают**

Run: `docker compose exec api python -m pytest tests/features/test_stratz_adapter.py -v`
Expected: FAIL — `ModuleNotFoundError: app.features.adapters.stratz`

- [ ] **Step 3: Реализовать адаптер**

```python
"""Adapter: STRATZ match -> GameState at a given minute (spec section 2.3, 6.4).

The train-time path, and since 27.08.2026 the only one that feeds `match_snapshots`.
OpenDota's per-minute series turned out to be *earned gold* while the live scoreboard the
serve path reads is *net worth*; STRATZ reports net worth, so train and serve finally
describe the same quantity. The measurements are in
docs/superpowers/specs/2026-08-27-stratz-adapter-design.md.

Nothing here reads a field that describes the end of the match. `towerStatusRadiant` and
`barracksStatusRadiant` are present in the payload and are exactly the trap the OpenDota
adapter documents: they are the final state, and putting them in a minute-15 row leaks the
result (spec section 12). Buildings are replayed from `towerDeaths` instead.
"""

from collections.abc import Mapping
from typing import Any

from app.features.buildings import BuildingState, state_at_npc
from app.features.game_state import GameState, SeriesContext, TeamState


def is_parsed(match: dict[str, Any]) -> bool:
    """No per-minute series without a parse, and `parsedDateTime` alone is not enough:
    it is occasionally set on matches whose series come back empty."""
    return bool(match.get("parsedDateTime")) and bool(match.get("radiantNetworthLeads"))


def _lead_at(series: list[int] | None, minute: int) -> int:
    """`radiantNetworthLeads` and `radiantExperienceLeads` carry an extra leading element
    for the time before the horn, so minute N sits at index N+1. Measured against
    OpenDota on real matches, not inferred from the array length."""
    if not series:
        return 0
    return int(series[min(minute + 1, len(series) - 1)])


def _at(series: list[int] | None, minute: int) -> int:
    """`networthPerMinute` is indexed by minute directly - no leading element."""
    if not series:
        return 0
    return int(series[min(minute, len(series) - 1)])


def _kills_through(series: list[int] | None, minute: int) -> int:
    """The kill arrays hold per-minute increments, so the score is their running sum."""
    if not series:
        return 0
    return sum(int(v) for v in series[: minute + 1])


def _players(match: dict[str, Any], radiant: bool) -> list[dict[str, Any]]:
    return [p for p in match.get("players") or [] if bool(p.get("isRadiant")) is radiant]


def _team_state_at(
    match: dict[str, Any], minute: int, radiant: bool, buildings: BuildingState
) -> TeamState:
    net_worths = tuple(
        _at((p.get("stats") or {}).get("networthPerMinute"), minute)
        for p in _players(match, radiant)
    )
    kills = match.get("radiantKills") if radiant else match.get("direKills")
    return TeamState(
        score=_kills_through(kills, minute),
        net_worth=sum(net_worths),
        towers_alive=buildings.towers,
        barracks_alive=buildings.barracks,
        ancient_alive=buildings.ancient_alive,
        player_net_worths=net_worths,
    )


def _picks(match: dict[str, Any], radiant: bool) -> tuple[int, ...]:
    """Hero ids of one side. Drafted before the horn, so known at every minute."""
    return tuple(int(p["heroId"]) for p in _players(match, radiant) if p.get("heroId"))


def snapshot_at(
    match: dict[str, Any],
    minute: int,
    series: SeriesContext | None = None,
    prematch_prior: float | None = None,
    prematch: Mapping[str, float] | None = None,
) -> GameState:
    """One training snapshot. Only information available at `minute` may be read."""
    deaths = match.get("towerDeaths") or []

    return GameState(
        match_id=int(match["id"]),
        minute=minute,
        radiant=_team_state_at(match, minute, True, state_at_npc(deaths, minute, radiant=True)),
        dire=_team_state_at(match, minute, False, state_at_npc(deaths, minute, radiant=False)),
        gold_adv=_lead_at(match.get("radiantNetworthLeads"), minute),
        xp_adv=_lead_at(match.get("radiantExperienceLeads"), minute),
        radiant_picks=_picks(match, radiant=True),
        dire_picks=_picks(match, radiant=False),
        series=series or SeriesContext(),
        prematch=prematch or {},
        prematch_prior=prematch_prior,
    )


def iter_snapshots(
    match: dict[str, Any],
    series: SeriesContext | None = None,
    min_minute: int = 0,
    prematch: Mapping[str, float] | None = None,
    prematch_prior: float | None = None,
) -> list[GameState]:
    """Unroll a parsed match into one snapshot per minute.

    Snapshots of one match are heavily correlated: any split must be by `match_id`,
    never by row (spec section 5.1).
    """
    if not is_parsed(match):
        raise ValueError(f"match {match.get('id')} is not parsed, no per-minute series")
    last_minute = int(match.get("durationSeconds", 0)) // 60
    return [
        snapshot_at(match, m, series, prematch_prior, prematch)
        for m in range(min_minute, last_minute + 1)
    ]
```

Рошановские поля `GameState` не заполняются намеренно — они уходят в дефолты и в вектор
больше не попадают (Task 3).

- [ ] **Step 4: Прогнать, убедиться, что проходят**

Run: `docker compose exec api python -m pytest tests/features/test_stratz_adapter.py -v`
Expected: PASS

- [ ] **Step 5: Коммит**

```bash
git add app/features/adapters/stratz.py tests/features/test_stratz_adapter.py
git commit -m "add STRATZ match adapter"
```

---

### Task 5: Тест на утечки и паритет с OpenDota

**Files:**
- Create: `tests/features/test_stratz_leakage.py`
- Create: `tests/features/test_source_parity.py`

**Interfaces:**
- Consumes: `app.features.adapters.stratz.{iter_snapshots, snapshot_at}`, `app.features.adapters.opendota` (как эталон), `app.features.live.build_live_features`

Тест на утечки — обязательный критерий фазы 3 (§11, §12) и должен существовать для каждого train-адаптера, а не только для OpenDota.

- [ ] **Step 1: Написать тест на утечки**

```python
# tests/features/test_stratz_leakage.py
"""The leakage test, for the STRATZ path (spec sections 5.1, 11, 12).

Same contract as tests/features/test_leakage.py, different payload shape. Features for
minute N must be identical whether computed from the whole match or from a match whose
recording was cut off just after minute N.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from app.features.adapters.stratz import snapshot_at
from app.features.live import build_live_features

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "stratz"
MATCHES = sorted(FIXTURES.glob("match_*.json"))


@pytest.fixture(params=MATCHES, ids=lambda p: p.stem)
def match(request: pytest.FixtureRequest) -> dict[str, Any]:
    return json.loads(request.param.read_text(encoding="utf-8"))


def truncate(match: dict[str, Any], minute: int) -> dict[str, Any]:
    """The same match as it would have looked with the recording stopped after `minute`.

    Everything a later minute could reveal is removed: the per-minute series, the building
    deaths, the final masks and the result itself.
    """
    cutoff = (minute + 1) * 60
    players = []
    for player in match.get("players") or []:
        trimmed = dict(player)
        stats = dict(trimmed.get("stats") or {})
        if stats.get("networthPerMinute"):
            stats["networthPerMinute"] = stats["networthPerMinute"][: minute + 1]
        trimmed["stats"] = stats
        players.append(trimmed)

    return {
        "id": match["id"],
        "parsedDateTime": match["parsedDateTime"],
        "durationSeconds": cutoff,
        "players": players,
        "radiantNetworthLeads": match["radiantNetworthLeads"][: minute + 2],
        "radiantExperienceLeads": match["radiantExperienceLeads"][: minute + 2],
        "radiantKills": match["radiantKills"][: minute + 1],
        "direKills": match["direKills"][: minute + 1],
        "towerDeaths": [e for e in match["towerDeaths"] if int(e["time"]) < cutoff],
    }


def test_features_do_not_change_when_the_future_is_removed(match: dict[str, Any]) -> None:
    last = match["durationSeconds"] // 60
    for minute in range(0, last + 1, max(1, last // 8)):
        whole = build_live_features(snapshot_at(match, minute))
        cut = build_live_features(snapshot_at(truncate(match, minute), minute))
        assert whole == cut, f"minute {minute} reads the future"
```

- [ ] **Step 2: Прогнать, убедиться, что проходит**

Run: `docker compose exec api python -m pytest tests/features/test_stratz_leakage.py -v`
Expected: PASS. Падение означает, что адаптер читает поле, описывающее конец матча.

- [ ] **Step 3: Написать тест паритета источников**

```python
# tests/features/test_source_parity.py
"""STRATZ against OpenDota on the same matches (spec section 6.4).

Only fields where both sources are authoritative are compared. Gold is deliberately NOT
among them: OpenDota's per-minute series is earned gold and STRATZ's is net worth, measured
and documented in docs/superpowers/specs/2026-08-27-stratz-adapter-design.md. Asserting
equality there would be asserting that two different quantities are the same number.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from app.features.adapters import opendota, stratz

OPENDOTA = Path(__file__).resolve().parent.parent / "fixtures" / "opendota"
STRATZ = Path(__file__).resolve().parent.parent / "fixtures" / "stratz"
PAIRS = sorted(
    (o, STRATZ / o.name) for o in OPENDOTA.glob("match_*.json") if (STRATZ / o.name).exists()
)


@pytest.fixture(params=PAIRS, ids=lambda p: p[0].stem)
def pair(request: pytest.FixtureRequest) -> tuple[dict[str, Any], dict[str, Any]]:
    left, right = request.param
    return (
        json.loads(left.read_text(encoding="utf-8")),
        json.loads(right.read_text(encoding="utf-8")),
    )


def test_the_fixtures_actually_pair_up() -> None:
    """A parity suite that silently compares nothing is worse than no parity suite."""
    assert len(PAIRS) >= 3


def test_same_duration_and_outcome(pair: tuple[dict[str, Any], dict[str, Any]]) -> None:
    od, st = pair
    assert od["duration"] == st["durationSeconds"]
    assert od["radiant_win"] is st["didRadiantWin"]


def test_same_picks(pair: tuple[dict[str, Any], dict[str, Any]]) -> None:
    od, st = pair
    for radiant in (True, False):
        assert sorted(opendota._picks(od, radiant)) == sorted(stratz._picks(st, radiant))


def test_same_buildings_every_minute(pair: tuple[dict[str, Any], dict[str, Any]]) -> None:
    """The npc id table is what makes this pass; a wrong entry shows up here as a tower
    that stands on one side and has fallen on the other."""
    od, st = pair
    for minute in range(0, od["duration"] // 60 + 1):
        left, right = opendota.snapshot_at(od, minute), stratz.snapshot_at(st, minute)
        assert left.radiant.towers_alive == right.radiant.towers_alive, minute
        assert left.dire.towers_alive == right.dire.towers_alive, minute
        assert left.radiant.barracks_alive == right.radiant.barracks_alive, minute
        assert left.dire.barracks_alive == right.dire.barracks_alive, minute


def test_same_kills_every_minute(pair: tuple[dict[str, Any], dict[str, Any]]) -> None:
    od, st = pair
    for minute in range(0, od["duration"] // 60 + 1):
        left, right = opendota.snapshot_at(od, minute), stratz.snapshot_at(st, minute)
        assert left.radiant.score == right.radiant.score, minute
        assert left.dire.score == right.dire.score, minute


def test_gold_advantage_agrees_on_sign_if_not_on_value(
    pair: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    """Different quantities, same story: whoever is ahead should be ahead in both. The
    threshold is 90% of minutes; the reconnaissance measured 215 of 230."""
    od, st = pair
    minutes = range(0, od["duration"] // 60 + 1)
    agree = sum(
        1
        for m in minutes
        if (opendota.snapshot_at(od, m).gold_adv > 0) == (stratz.snapshot_at(st, m).gold_adv > 0)
    )
    assert agree / len(minutes) >= 0.9
```

- [ ] **Step 4: Прогнать паритет**

Run: `docker compose exec api python -m pytest tests/features/test_source_parity.py -v`
Expected: PASS. Падение `test_same_buildings_every_minute` — ошибка в таблице npcId;
падение `test_same_kills_every_minute` — сбитое выравнивание массивов килов.

- [ ] **Step 5: Коммит**

```bash
git add tests/features/test_stratz_leakage.py tests/features/test_source_parity.py
git commit -m "add leakage and cross-source parity tests for the STRATZ path"
```

---

### Task 6: Воркер деталей, параметризованный источником

**Files:**
- Modify: `app/ingestion/workers/details.py`
- Modify: `tests/ingestion/test_details.py`

**Interfaces:**
- Produces:
  - `select_matches_missing_details(session, limit, newest_first=True, source=RawSource.OPENDOTA_MATCH) -> list[int]`
  - `count_missing_details(session, source=RawSource.OPENDOTA_MATCH) -> int`
  - `run_details_backfill(client, session_factory, limit=100, newest_first=True, source=RawSource.OPENDOTA_MATCH) -> DetailsReport`
  - `backfill_match_details(ctx, limit=100, newest_first=True, source="stratz"|"opendota") -> int`
- `MatchDetailSource` (Protocol с `async def match(self, match_id: int) -> dict[str, Any]`) уже подходит обоим клиентам без изменений.

Значения по умолчанию оставлены на OpenDota, чтобы существующие вызовы и тесты не поменяли смысл молча; переключение делается явно из CLI.

- [ ] **Step 1: Написать падающий тест**

```python
# добавить в tests/ingestion/test_details.py
async def test_missing_details_are_counted_per_source(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A map fetched from OpenDota is still missing from STRATZ, and the other way round.
    One shared counter would report the backfill as finished when half of it had not run."""
    async with sessionmaker() as session:
        session.add_all([Match(match_id=1, start_time=_utc()), Match(match_id=2, start_time=_utc())])
        await session.commit()
        await upsert_raw_matches(session, RawSource.OPENDOTA_MATCH, [{"match_id": 1}])
        await session.commit()

    async with sessionmaker() as session:
        assert await count_missing_details(session, RawSource.OPENDOTA_MATCH) == 1
        assert await count_missing_details(session, RawSource.STRATZ_MATCH) == 2
        assert await select_matches_missing_details(session, 10, source=RawSource.OPENDOTA_MATCH) == [2]
        assert sorted(
            await select_matches_missing_details(session, 10, source=RawSource.STRATZ_MATCH)
        ) == [1, 2]


async def test_backfill_writes_under_the_requested_source(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker() as session:
        session.add(Match(match_id=5, start_time=_utc()))
        await session.commit()

    class Client:
        async def match(self, match_id: int) -> dict[str, Any]:
            return {"id": match_id, "durationSeconds": 1800}

    report = await run_details_backfill(
        Client(), sessionmaker, limit=10, source=RawSource.STRATZ_MATCH
    )

    assert report.fetched == 1
    async with sessionmaker() as session:
        assert await count_raw_matches(session, RawSource.STRATZ_MATCH) == 1
        assert await count_raw_matches(session, RawSource.OPENDOTA_MATCH) == 0
```

`_utc()` — существующий помощник в `tests/ingestion/test_details.py`; если его там нет,
использовать тот способ построения `start_time`, что уже применяют соседние тесты файла.

- [ ] **Step 2: Прогнать, убедиться, что падает**

Run: `docker compose exec api python -m pytest tests/ingestion/test_details.py -v`
Expected: FAIL — `TypeError: count_missing_details() takes 1 positional argument but 2 were given`

- [ ] **Step 3: Параметризовать воркер**

В `app/ingestion/workers/details.py`:

```python
async def select_matches_missing_details(
    session: AsyncSession,
    limit: int,
    newest_first: bool = True,
    source: RawSource = RawSource.OPENDOTA_MATCH,
) -> list[int]:
    """Maps we know about but have no detail payload for, from this source.

    Per source, not shared: a map fetched from OpenDota is still missing from STRATZ, and
    a single counter would call the backfill finished with half of it unrun.

    Newest first by default: recent patches are the ones the model is asked about, and a
    backfill that is stopped halfway should have covered the most useful history.
    """
    already = select(RawMatch.match_id).where(RawMatch.source == str(source))
    ...
```

Те же изменения в `count_missing_details`. В `run_details_backfill` добавить параметр
`source: RawSource = RawSource.OPENDOTA_MATCH`, прокинуть его в оба вызова
`select_matches_missing_details` / `count_missing_details` и в `upsert_raw_matches`,
а в `log.info("details.start", ...)` и `report.as_log_fields()` добавить `source=str(source)`,
чтобы в логе было видно, какой прогон идёт.

В `backfill_match_details` (точка входа arq) выбрать клиента по источнику:

```python
async def backfill_match_details(
    ctx: dict[str, Any], limit: int = 100, newest_first: bool = True, source: str = "stratz"
) -> int:
    """arq entry point. Returns payloads fetched."""
    raw_source = RawSource.STRATZ_MATCH if source == "stratz" else RawSource.OPENDOTA_MATCH
    client_type = StratzClient if source == "stratz" else OpenDotaClient
    async with client_type() as client:
        report = await run_details_backfill(
            client, get_session_factory(), limit=limit, newest_first=newest_first, source=raw_source
        )
    return report.fetched
```

Дополнить шапку модуля: суточный лимит — про OpenDota, у STRATZ ограничение часовое
(~2000 req/час), и именно поэтому он теперь основной.

- [ ] **Step 4: Прогнать тесты воркера**

Run: `docker compose exec api python -m pytest tests/ingestion/test_details.py -v`
Expected: PASS, включая существующие тесты — умолчания не менялись.

- [ ] **Step 5: Коммит**

```bash
git add app/ingestion/workers/details.py tests/ingestion/test_details.py
git commit -m "let the details backfill target either source"
```

---

### Task 7: Нормализация STRATZ-payload

**Files:**
- Modify: `app/ingestion/normalize.py`
- Modify: `tests/ingestion/test_normalize.py`

**Interfaces:**
- Produces: `parse_stratz_match_detail(payload: dict[str, Any]) -> dict[str, list[dict[str, Any]]]` — те же три ключа `{"players", "drafts", "objectives"}` и те же имена колонок, что у существующего `parse_match_detail`, чтобы `_upsert_composite` не менялся.
- `normalize_match_details` читает обе `RawSource` и выбирает разборщик по строке источника.

STRATZ-эквиваленты полей: `playerSlot` → `player_slot`, `steamAccountId` → `account_id`,
`heroId` → `hero_id`, `numLastHits`/`numDenies` → `last_hits`/`denies`,
`goldPerMinute`/`experiencePerMinute` → `gold_per_min`/`xp_per_min`, `networth` → `net_worth`,
`leaverStatus` → `leaver_status`, `lane` → `lane_role`. Драфт — из `pickBans`
(`order`, `isPick`, `heroId`/`bannedHeroId`, `isRadiant` → `team` 0/1).
`objectives` из STRATZ — только события строений из `towerDeaths`, с типом `building_kill`
и ключом-именем здания, чтобы таблица оставалась однородной.

- [ ] **Step 1: Написать падающий тест**

```python
# добавить в tests/ingestion/test_normalize.py
from app.ingestion.normalize import parse_stratz_match_detail

STRATZ_PAYLOAD = {
    "id": 42,
    "durationSeconds": 1800,
    "didRadiantWin": True,
    "players": [
        {
            "playerSlot": 0, "steamAccountId": 111, "heroId": 8, "isRadiant": True,
            "kills": 5, "deaths": 1, "assists": 3, "numLastHits": 200, "numDenies": 10,
            "networth": 20000, "goldPerMinute": 600, "experiencePerMinute": 700,
            "leaverStatus": "NONE", "lane": "SAFE_LANE",
        },
        {
            "playerSlot": 128, "steamAccountId": 222, "heroId": 9, "isRadiant": False,
            "kills": 1, "deaths": 5, "assists": 0, "numLastHits": 100, "numDenies": 2,
            "networth": 12000, "goldPerMinute": 400, "experiencePerMinute": 450,
            "leaverStatus": "NONE", "lane": "OFF_LANE",
        },
    ],
    "pickBans": [
        {"order": 0, "isPick": False, "bannedHeroId": 14, "heroId": None, "isRadiant": True},
        {"order": 1, "isPick": True, "heroId": 8, "isRadiant": True},
    ],
    "towerDeaths": [
        {"time": 600, "npcId": 16, "isRadiant": True},
        {"time": 700, "npcId": 36, "isRadiant": True},
    ],
}


class TestParseStratzMatchDetail:
    def test_players_map_onto_the_same_columns_as_opendota(self) -> None:
        rows = parse_stratz_match_detail(STRATZ_PAYLOAD)["players"]
        assert len(rows) == 2
        radiant = next(r for r in rows if r["player_slot"] == 0)
        assert radiant["account_id"] == 111
        assert radiant["hero_id"] == 8
        assert radiant["is_radiant"] is True
        assert radiant["last_hits"] == 200
        assert radiant["net_worth"] == 20000
        assert radiant["gold_per_min"] == 600
        assert radiant["xp_per_min"] == 700

    def test_draft_carries_bans_and_picks(self) -> None:
        rows = parse_stratz_match_detail(STRATZ_PAYLOAD)["drafts"]
        assert {r["order"]: (r["is_pick"], r["hero_id"], r["team"]) for r in rows} == {
            0: (False, 14, 0),
            1: (True, 8, 0),
        }

    def test_objectives_are_building_kills_with_unknown_ids_dropped(self) -> None:
        rows = parse_stratz_match_detail(STRATZ_PAYLOAD)["objectives"]
        assert len(rows) == 1
        assert rows[0]["type"] == "building_kill"
        assert rows[0]["time"] == 600
        assert rows[0]["key"] == "npc_dota_goodguys_tower1_top"

    def test_series_membership_is_never_touched(self) -> None:
        """Invariant 11: series membership is owned by the /proMatches summary layer.
        The detail parser must not produce anything that could overwrite it."""
        parsed = parse_stratz_match_detail(STRATZ_PAYLOAD)
        assert set(parsed) == {"players", "drafts", "objectives"}
        for rows in parsed.values():
            for row in rows:
                assert "series_id" not in row
```

- [ ] **Step 2: Прогнать, убедиться, что падает**

Run: `docker compose exec api python -m pytest tests/ingestion/test_normalize.py -v`
Expected: FAIL — `ImportError: cannot import name 'parse_stratz_match_detail'`

- [ ] **Step 3: Реализовать разборщик**

```python
# app/ingestion/normalize.py, рядом с parse_match_detail

#: STRATZ names a destroyed building by npc id; OpenDota names it by npc name, and that is
#: what `match_objectives.key` already holds. Rendering the id back into the same name keeps
#: one vocabulary in the table rather than two that mean the same thing.
_SIDE_NAMES = {True: "goodguys", False: "badguys"}
_TIER_BY_COUNT = {3: "tower3", 2: "tower2", 1: "tower1"}


def _building_name(kill: BuildingKill, npc_id: int) -> str:
    side = _SIDE_NAMES[kill.is_radiant]
    if kill.kind == "ancient":
        return f"npc_dota_{side}_fort"
    if kill.lane == BASE:
        return f"npc_dota_{side}_tower4"
    if kill.kind == "tower":
        tier = (npc_id - (16 if kill.is_radiant else 26)) // 3 + 1
        return f"npc_dota_{side}_tower{tier}_{kill.lane}"
    melee = (npc_id - (38 if kill.is_radiant else 44)) < 3
    return f"npc_dota_{side}_{'melee' if melee else 'range'}_rax_{kill.lane}"


def parse_stratz_match_detail(payload: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Split one STRATZ `match(id:)` payload into rows for the normalized tables.

    Same three keys and the same column names as `parse_match_detail`, so both sources
    land in the same tables through the same upserts.

    Note what is NOT read here, exactly as on the OpenDota side: `seriesId` is present in
    this payload, and series membership is owned by the /proMatches summaries. Writing it
    from a detail parse would let the weaker source overwrite the stronger one
    (invariant 11).
    """
    match_id = int(payload["id"])

    players = [
        {
            "match_id": match_id,
            "player_slot": int(player["playerSlot"]),
            "account_id": player.get("steamAccountId"),
            "hero_id": player.get("heroId"),
            "is_radiant": bool(player.get("isRadiant")),
            "lane_role": player.get("lane"),
            "kills": player.get("kills"),
            "deaths": player.get("deaths"),
            "assists": player.get("assists"),
            "last_hits": player.get("numLastHits"),
            "denies": player.get("numDenies"),
            "net_worth": player.get("networth"),
            "gold_per_min": player.get("goldPerMinute"),
            "xp_per_min": player.get("experiencePerMinute"),
            "leaver_status": player.get("leaverStatus"),
            "is_standin": False,  # needs roster history from Liquipedia (phase 2)
        }
        for player in payload.get("players") or []
        if player.get("playerSlot") is not None
    ]

    drafts = []
    for entry in payload.get("pickBans") or []:
        hero_id = entry.get("heroId") if entry.get("isPick") else entry.get("bannedHeroId")
        if entry.get("order") is None or hero_id is None:
            continue
        drafts.append(
            {
                "match_id": match_id,
                "order": int(entry["order"]),
                "is_pick": bool(entry["isPick"]),
                "hero_id": int(hero_id),
                # Valve numbers radiant 0 and dire 1 in the draft log.
                "team": 0 if entry.get("isRadiant") else 1,
            }
        )

    objectives = []
    for event in payload.get("towerDeaths") or []:
        npc_id = int(event.get("npcId") or 0)
        kill = parse_npc_id(npc_id)
        if kill is None:  # ids 36 and 37 are not buildings we track
            continue
        objectives.append(
            {
                "match_id": match_id,
                "ordinal": len(objectives),
                "time": int(event.get("time", 0)),
                "type": "building_kill",
                # Valve numbers radiant 2 and dire 3 in the objectives log.
                "team": 2 if kill.is_radiant else 3,
                "key": _building_name(kill, npc_id),
                "player_slot": None,
            }
        )

    return {"players": players, "drafts": drafts, "objectives": objectives}
```

Импорты, которые для этого нужны в `normalize.py`:
`from app.features.buildings import BASE, BuildingKill, parse_npc_id`.

В `normalize_match_details` расширить выборку на обе `RawSource` и выбирать разборщик
по источнику строки:

```python
_DETAIL_PARSERS = {
    str(RawSource.OPENDOTA_MATCH): parse_match_detail,
    str(RawSource.STRATZ_MATCH): parse_stratz_match_detail,
}
```

в запросе — `select(RawMatch.source, RawMatch.payload).where(RawMatch.source.in_(_DETAIL_PARSERS))`,
в цикле — `parsed = _DETAIL_PARSERS[source](payload)`. Инвариант №11 сохраняется:
ни один из разборщиков не отдаёт `series_id`.

`_enrich_matches` заполняет `matches.patch` из OpenDota-поля `patch`; у STRATZ его
эквивалент — `gameVersionId`, и это другая нумерация. Поэтому для STRATZ-строк
`_enrich_matches` не вызывать: пусть `patch` останется `NULL`, а не будет заполнен числом
из чужой шкалы (инвариант №12).

- [ ] **Step 4: Прогнать тесты нормализации**

Run: `docker compose exec api python -m pytest tests/ingestion/test_normalize.py -v`
Expected: PASS, включая существующие тесты OpenDota-пути.

- [ ] **Step 5: Коммит**

```bash
git add app/ingestion/normalize.py tests/ingestion/test_normalize.py
git commit -m "normalize STRATZ match payloads into the shared tables"
```

---

### Task 8: `featurize` строит снапшоты из STRATZ

**Files:**
- Modify: `app/features/featurize.py`
- Modify: `tests/features/` — тест `featurize`, если есть; иначе создать `tests/features/test_featurize.py`

**Interfaces:**
- Consumes: `app.features.adapters.stratz.{is_parsed, iter_snapshots}`
- Produces: поведение `featurize(session_factory, batch_size=50, limit=None)` не меняется по сигнатуре — меняется источник

Поля payload другие, поэтому меняются и три проверки отбраковки: `match_id` → `id`,
`radiant_win` → `didRadiantWin`, `duration` → `durationSeconds`.

- [ ] **Step 1: Написать падающий тест**

```python
# tests/features/test_featurize.py
async def test_snapshots_are_built_from_stratz_payloads(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    payload = json.loads(
        (Path(__file__).resolve().parent.parent / "fixtures" / "stratz" / "match_8946228708.json")
        .read_text(encoding="utf-8")
    )
    async with sessionmaker() as session:
        session.add(Match(match_id=int(payload["id"]), start_time=_utc()))
        await session.commit()
        await upsert_raw_matches(session, RawSource.STRATZ_MATCH, [payload])
        await session.commit()

    report = await featurize(sessionmaker)

    assert report.matches_used == 1
    assert report.snapshots == payload["durationSeconds"] // 60 + 1
    async with sessionmaker() as session:
        rows = (await session.execute(select(MatchSnapshot))).scalars().all()
    assert {r.minute for r in rows} == set(range(payload["durationSeconds"] // 60 + 1))
    assert all(set(r.features) == set(FEATURE_ORDER) for r in rows)
    assert all(r.radiant_win is payload["didRadiantWin"] for r in rows)


async def test_short_matches_are_skipped(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Spec section 5.3: filtered by metadata, never by outcome."""
    async with sessionmaker() as session:
        await upsert_raw_matches(
            session,
            RawSource.STRATZ_MATCH,
            [{"id": 99, "parsedDateTime": 1, "radiantNetworthLeads": [0, 1],
              "didRadiantWin": True, "durationSeconds": 300, "players": []}],
        )
        await session.commit()

    report = await featurize(sessionmaker)

    assert report.matches_used == 0
    assert report.skipped == {"shorter than 12 minutes": 1}
```

- [ ] **Step 2: Прогнать, убедиться, что падает**

Run: `docker compose exec api python -m pytest tests/features/test_featurize.py -v`
Expected: FAIL — `report.matches_used == 0`, потому что `featurize` читает `opendota_match`

- [ ] **Step 3: Переключить `featurize`**

В `app/features/featurize.py`: импорт адаптера сменить на
`from app.features.adapters.stratz import is_parsed, iter_snapshots`; в выборке
`RawMatch.source == str(RawSource.STRATZ_MATCH)`; в `_featurize_batch` заменить обращения
к полям на STRATZ-имена (`payload.get("id")`, `payload.get("didRadiantWin")`,
`payload.get("durationSeconds")`).

В шапке модуля дописать, почему источник именно этот, со ссылкой на спеку дизайна:
OpenDota-ряд — добытое золото, STRATZ-ряд — net worth, live-канал даёт net worth.

- [ ] **Step 4: Прогнать**

Run: `docker compose exec api python -m pytest tests/features/ -v`
Expected: PASS

- [ ] **Step 5: Коммит**

```bash
git add app/features/featurize.py tests/features/test_featurize.py
git commit -m "build snapshots from STRATZ payloads"
```

---

### Task 9: CLI, статус и документация

**Files:**
- Modify: `app/ingestion/cli.py`
- Modify: `CLAUDE.md`, `../CLAUDE.md`
- Modify: `docs/spec.md` §2.3 (и копия во фронтенд-репозитории, и `dota2-prediction-spec.md`)

**Interfaces:**
- Produces: `python -m app.ingestion.cli details --source stratz|opendota --limit N [--oldest-first]`, по умолчанию `stratz`

- [ ] **Step 1: Добавить флаг и выбор клиента**

```python
# app/ingestion/cli.py

#: Measured for OpenDota (~16.5 maps a minute, bound by response time). For STRATZ the
#: ceiling is the hourly allowance rather than latency: ~2000 requests an hour is ~33 a
#: minute, and that is the number a run actually converges to.
FETCH_PER_MINUTE = {"opendota": 16.5, "stratz": 33.0}


async def cmd_details(limit: int, oldest_first: bool, source: str) -> None:
    raw_source = RawSource.STRATZ_MATCH if source == "stratz" else RawSource.OPENDOTA_MATCH
    client_type = StratzClient if source == "stratz" else OpenDotaClient

    async with client_type() as client:
        report = await run_details_backfill(
            client,
            get_session_factory(),
            limit=limit,
            newest_first=not oldest_first,
            source=raw_source,
        )

    print(f"source:    {source}")
    print(f"requested: {report.requested}")
    print(f"fetched:   {report.fetched}")
    print(f"failed:    {report.failed}")
    print(f"stopped:   {report.stopped_because}")
    print(f"remaining: {report.remaining}")
    if report.remaining:
        rate = FETCH_PER_MINUTE[source]
        print(f"           (~{report.remaining / rate / 60:.1f} h at ~{rate} maps/min)")
```

В `main()` добавить

```python
    details.add_argument(
        "--source",
        choices=("stratz", "opendota"),
        default="stratz",
        help="where to fetch per-minute series from (default: stratz)",
    )
```

и прокинуть `args.source` в `cmd_details`.

- [ ] **Step 2: Показать оба счётчика в `status`**

```python
# в cmd_status, вместо одиночных details / missing
    async with get_session_factory()() as session:
        cursor = await get_checkpoint(session, Checkpoint.OPENDOTA_PRO_MATCHES)
        summaries = await count_raw_matches(session, RawSource.OPENDOTA_PRO_MATCHES)
        total = await count_raw_matches(session)
        normalized = await normalized_counts(session)
        per_source = {
            name: (
                await count_raw_matches(session, raw_source),
                await count_missing_details(session, raw_source),
            )
            for name, raw_source in (
                ("stratz", RawSource.STRATZ_MATCH),
                ("opendota", RawSource.OPENDOTA_MATCH),
            )
        }

    print(f"checkpoint (oldest match_id seen): {cursor or '-'}")
    print(f"raw pro-match summaries:           {summaries}")
    print(f"raw rows total:                    {total}")
    print()
    # Per source, not merged: a map fetched from OpenDota is still missing from STRATZ, and
    # one shared counter would report the backfill as finished with half of it unrun.
    for name, (fetched, missing) in per_source.items():
        print(f"{name + ' payloads':<34} {fetched}")
        print(f"{name + ' maps still missing':<34} {missing}")
    print()
    for label, count in normalized.items():
        print(f"{label:<34} {count}")
```

- [ ] **Step 3: Проверить вручную**

```bash
docker compose exec api python -m app.ingestion.cli status
docker compose exec api python -m app.ingestion.cli details --source stratz --limit 5
docker compose exec api python -m app.ingestion.cli status
```

Expected: `details` печатает `fetched: 5`, второй `status` показывает на 5 меньше
недостающих у STRATZ и столько же, сколько было, у OpenDota.

- [ ] **Step 4: Обновить документацию**

`docs/spec.md` §2.3: заменить «Разделение ролей» — поминутные ряды берутся из STRATZ,
а не из OpenDota; отметить, что `playbackData` на бесплатном токене пуст и что
`matches(ids:)` требует админского токена, поэтому выборка поштучная. Ту же правку внести
в обе копии спеки.

`CLAUDE.md` бэкенда: обновить карту репозитория (`adapters/stratz.py`), раздел про лимиты
источников, порядок команд пайплайна.

`../CLAUDE.md`: переписать «Текущий шаг» — блокер снят, путь №1 выбран и реализован;
обновить порядок команд; убрать из «трёх путей» уже принятое решение.

- [ ] **Step 5: Коммит**

```bash
git add app/ingestion/cli.py docs/spec.md CLAUDE.md
git commit -m "add source flag to the details command"
```

Правку `../CLAUDE.md` и копию спеки во фронтенде коммитить в их собственных репозиториях.

---

### Task 10: Перекачка и пересборка выборки

**Files:** данные, не код.

Этот шаг делает выборку однородной. До него в базе смесь: 685 карт разобраны из OpenDota,
остальные не разобраны вовсе.

- [ ] **Step 1: Полный прогон проверок**

Run: `docker compose exec api ruff check . && docker compose exec api ruff format --check . && docker compose exec api mypy app && docker compose exec api python -m pytest -q`
Expected: всё зелёное. Без этого перекачку не начинать — она занимает часы.

- [ ] **Step 2: Скачать детали через STRATZ**

```bash
docker compose exec api python -m app.ingestion.cli details --source stratz --limit 700
docker compose exec api python -m app.ingestion.cli status
```

Ожидание: ~700 карт за ~21 минуту при лимите ~2000 запросов в час. `RateLimitedError`
останавливает прогон, а не продолжает долбить API; повторный запуск продолжает с места
остановки, потому что уже сохранённое и есть чекпойнт.

- [ ] **Step 3: Пересобрать нормализованный слой и признаки**

```bash
docker compose exec api python -m app.ingestion.cli normalize
docker compose exec api python -m app.ingestion.cli prematch    # строго до featurize
docker compose exec api python -m app.ingestion.cli featurize
docker compose exec api python -m app.ingestion.cli status
```

`prematch` строго перед `featurize` — иначе снапшоты получат нулевой prior.

- [ ] **Step 4: Проверить результат**

```bash
docker compose exec -T api python - <<'PY'
import asyncio
from sqlalchemy import text
from app.db.session import get_session_factory, dispose_engine
async def main():
    async with get_session_factory()() as s:
        for q in ("select count(*) from match_snapshots",
                  "select count(distinct match_id) from match_snapshots",
                  "select count(*) from raw_matches where source='stratz_match'"):
            print(q, "->", (await s.execute(text(q))).scalar())
        row = (await s.execute(text("select features from match_snapshots limit 1"))).scalar()
        print("feature count ->", len(row))
    await dispose_engine()
asyncio.run(main())
PY
```

Expected: `feature count -> 29`, число матчей в снапшотах равно числу скачанных через
STRATZ разобранных карт, снапшотов заметно больше прежних 23 289.

- [ ] **Step 5: Продолжать фоном**

Дальнейшая закачка — тот же `details --source stratz` повторно, пока `status` не покажет
ноль недостающих. Это уже эксплуатация, а не часть ветки.

---

## Что этот план не делает

- Не обучает модель. Фаза 4 — следующая задача.
- Не трогает `matches(ids:)` — он требует админского токена.
- Не возвращает рошана. Если тариф токена STRATZ откроет `playbackData`, это отдельная
  задача с возвратом трёх признаков в `FEATURE_ORDER`, обоих адаптеров и спеки.
- Не снимает `skip` с регрессионного теста train/serve parity (§6.4): для него нужны
  парные live-снапшоты и разбор того же завершённого матча. После этой ветки он впервые
  становится осмысленным — обе стороны считают net worth.
