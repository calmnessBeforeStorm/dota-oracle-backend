# CLAUDE.md — dota-oracle-backend

Бэкенд системы прогнозирования исходов профессиональных матчей Dota 2.
Полная спецификация: [docs/spec.md](docs/spec.md) — **читай её перед любой содержательной задачей**,
ссылки вида «§5.5» ниже указывают на её разделы.

## Что это

Сервис в реальном времени оценивает вероятность победы каждой команды в идущих матчах
Tier 1 и отдаёт турнирный календарь. Две разные ML-задачи, смешивать их нельзя:

| | Pre-match | **Live (основная)** |
|---|---|---|
| Когда | до старта карты | каждые 20–30 сек по ходу игры |
| Роль в v1 | вспомогательная, даёт prior | **основной продукт** |

Единица прогноза — **карта**, у неё всегда есть победитель. Серия — отдельный уровень (§5.5).

## Текущее состояние

Фаза 0 («Скелет») по дорожной карте §11. Схема БД, адаптеры признаков, клиенты источников
и API-контуры заложены; тела воркеров и ML-пайплайна помечены `TODO(phase-N)`.
Модель по умолчанию — `BaselinePredictor` (логрегрессия на `gold_adv` и `minute`), она же
бейзлайн №3 из §7.3: настоящая модель обязана её заметно бить.

## Карта репозитория

```
app/
├── main.py                  FastAPI factory, lifespan (модель грузится на старте)
├── core/                    config (pydantic-settings), structlog, redis (кэш + pub/sub)
├── db/
│   ├── base.py              DeclarativeBase, naming convention, TimestampMixin (UTC)
│   └── models/
│       ├── enums.py         SeriesFormat (bo1/bo2/bo3/bo5), StageType, LeagueTier
│       ├── raw.py           §4.1 сырьё + ingest_checkpoints
│       ├── reference.py     leagues, tournament_stages, teams, players, team_rosters
│       ├── matches.py       series, matches, match_players/drafts/objectives
│       └── training.py      match_snapshots, player_ratings, team_features, predictions
├── domain/series.py         правила серий: формат, Bo2, ничьи, is_conditional_game (§5.5)
├── features/
│   ├── game_state.py        GameState — единая промежуточная структура
│   ├── live.py              ЕДИНСТВЕННЫЙ билдер признаков + FEATURE_ORDER
│   └── adapters/            opendota.py (train) и steam.py (serve) → GameState
├── ingestion/
│   ├── clients/             base (throttle + retry), opendota, stratz, steam, liquipedia
│   └── workers/             backfill, live_poller, sync
├── ml/                      predictor (инференс в процессе API), registry, pipeline
├── schemas/                 pydantic-схемы API
├── api/routes/              health, matches, tournaments, teams, model, ws
└── workers/settings.py      arq WorkerSettings + cron
```

## Инварианты — нарушать нельзя

1. **Никакой информации из будущего в признаках.** Всё считается point-in-time. Пример
   ловушки: `tower_status_radiant` в ответе OpenDota — это состояние на конец матча, в
   снапшот 15-й минуты его класть нельзя.
2. **Признаки считаются в одном месте** — `app/features/live.py`, из `GameState`. Оба
   источника (OpenDota и Steam) приводятся к `GameState` адаптерами. Иначе train/serve skew
   гарантирован (§6.4). Новый признак → `FEATURE_ORDER` + билдер + оба адаптера + тест.
3. **Любой сплит — по `match_id`**, никогда по строкам: снапшоты одного матча сильно
   коррелированы (§5.1). Валидация только walk-forward по времени.
4. **Сырьё храним целиком и навсегда** (`raw_*`). Признаки будут переразбираться десятки
   раз, перекачивать данные нельзя — квоты, время, риск исчезновения источника.
5. **Все ingestion-операции идемпотентны** (upsert по естественным ключам), прогресс — в
   `ingest_checkpoints`. Перезапуск воркера не должен порождать дубликаты.
6. **Все временные метки — UTC.**
7. **Не фильтруем выборку по исходу.** Выкидывание «подозрительных сливов» даёт модель,
   которая не знает о камбэках (§5.3).
8. **Каждый выданный прогноз пишется в `predictions`** с `model_version` и `features`.
   Без этого падение качества через месяц необъяснимо.

## Серии, Bo2 и ничьи (§5.5) — источник частых ошибок

- `series_type` от Valve **ненадёжен**: Bo2 в нём не представлен (приходит `0`), и отличить
  Bo2 от двух независимых Bo1 по данным Steam/OpenDota невозможно.
- Источник истины по формату — **Liquipedia**, через `tournament_stages.default_format`.
  Поле `format` дублируется на самой серии: тай-брейки и переигровки отклоняются от
  дефолта стадии. Порядок разрешения — в `app/domain/series.resolve_format()`.
- `series.winner_team_id` **nullable** + отдельный флаг `is_draw`. «Ещё не решено» и
  «ничья 1–1» — разные состояния, путать их в UI нельзя.
- Признак `game_in_series` подавать только вместе с `series_format` и
  `is_conditional_game`. В Bo3 третья карта играется лишь при 1–1, то есть выборка третьих
  карт смещена; без флага модель выучит артефакт формата.
- Начисление турнирных очков за Bo2 — конфигурация в `tournament_stages.points_rule`,
  **не хардкод**: регламенты разные.
- Вероятность исхода Bo2 — трёхзначная (2–0 / 1–1 / 0–2), мультиномиальная. Наивная свёртка
  `p²/2p(1−p)/(1−p)²` — только отправная точка: карты не независимы, реальная доля 1–1 выше.

## Live-источник: спека расходится с реальностью (проверено 26.08.2026)

Спека описывала связку C1 → C2: из `GetLiveLeagueGames` берём `server_steam_id`, по нему
дёргаем `GetRealtimeStats`. **На живом API это не работает.**

- `server_steam_id` не пришёл **ни в одной из 37** идущих турнирных игр.
- `lobby_id` не заменяет его: `GetRealtimeStats` отвечает `400 Bad Request`.
- `GetTopLiveGame` отдаёт `server_steam_id` (10/10), но это топ игр по MMR, а не Tier 1.

**Основной канал live-состояния — C1 (`GetLiveLeagueGames`), а не C2.** Его `scoreboard`
самодостаточен: по игроку — `net_worth`, `gold`, `level`, `xp_per_min`, `gold_per_min`,
`kills`/`death`/`assists`, `last_hits`, `denies`; по команде — `score`, `tower_state`,
`barracks_state`, `picks`, `bans`; по игре — `duration`, `roshan_respawn_timer`.

Два следствия для кода:

1. `tower_state` и `barracks_state` — **битовые маски того же формата, что
   `tower_status_radiant` / `barracks_status_radiant` у OpenDota**. Декодер писать один,
   общий для обоих адаптеров, — это прямой выигрыш для train/serve parity (§6.4).
2. `gold_adv` на live-стороне считается суммой `net_worth` по командам, а не из
   `graph_gold` (ряда в C1 нет). Расхождение с офлайн-расчётом обязано быть покрыто
   регрессионным тестом.

`raw_live_snapshots.server_steam_id` поэтому nullable, а `source` различает канал.

## Внешние источники и их лимиты (§2)

| Источник | Роль | Лимит, который реально бьёт |
|---|---|---|
| OpenDota | датасет, поминутные ряды | 50 000 вызовов/мес, 60 req/min |
| STRATZ | массовый бэкфилл «скелета» истории | ~2000 req/час |
| Steam | **live-состояние**, без него продукта нет | мягкие, ~100k/сут |
| Liquipedia | тиры, расписание, **форматы серий** | ~1 req/2 сек, `parse` ~1/30 сек |

Liquipedia банит по IP: кастомный `User-Agent` с контактом обязателен, кэш обязателен,
атрибуция CC-BY-SA на страницах с их данными обязательна (§13). Троттлинг зашит в
`app/ingestion/clients/base.py` — новые клиенты наследовать оттуда, а не писать httpx руками.

## Команды

```bash
cp .env.example .env            # STEAM_API_KEY нужен для live-контура
docker compose up               # postgres + redis + api + worker
curl localhost:8000/api/health

pip install -e ".[dev]"         # локально, Python 3.12
pytest                          # тесты
ruff check . && ruff format .   # линт
mypy app                        # типы (strict)

alembic revision --autogenerate -m "описание"   # миграция
alembic upgrade head
```

Docs API: `http://localhost:8000/docs`.

## Правила ведения репозитория

Общие правила монорепо-пары описаны в `../CLAUDE.md`, коротко:

- Дефолтная ветка — **`development`** (тестовый сервер). Вся работа ветвится от неё.
- Каждая фича — отдельная ветка от `development`: `feature/<кратко>`, `fix/<кратко>`.
  Прямых коммитов в `development` нет, только мердж.
- **В `main` категорически нельзя коммитить и пушить.** Единственный путь кода в прод —
  мердж `development` → `main`, и он делается **только по явному разрешению владельца
  проекта на этот конкретный мердж**. Разрешение не переносится на следующие разы.
  По своей инициативе в `main` не ходить и не предлагать этого.
- Коммиты подписываются почтой **`ersaim.adilet@yandex.kz`** (в репозитории уже прописан
  локальный `user.email`, не перезаписывать глобальным).
- **Сообщения коммитов — на английском**, в императиве: `add live match poller`, а не
  `added changes`. Трейлеры об авторстве ИИ не добавляются.
- Перед пушем прогнать то же, что гоняет CI:
  `ruff check . && ruff format --check . && mypy app && pytest`.
  `mypy` здесь в режиме `strict` — он ловит то, чего не видят тесты (например, вызовы
  неаннотированных функций из сторонних библиотек).
