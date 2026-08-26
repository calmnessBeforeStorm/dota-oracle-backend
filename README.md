# dota-oracle-backend

Оценка вероятности победы в идущих матчах Dota 2 уровня Tier 1 + турнирный календарь.
FastAPI · PostgreSQL 16 · Redis 7 · arq · LightGBM.

Спецификация проекта: [docs/spec.md](docs/spec.md).
Инструкции для агентов и инварианты кодовой базы: [CLAUDE.md](CLAUDE.md).

## Быстрый старт

```bash
cp .env.example .env
docker compose up
curl localhost:8000/api/health
```

Поднимаются `postgres`, `redis`, `api` (uvicorn с автоперезагрузкой) и `worker` (arq).
Swagger — `http://localhost:8000/docs`.

### Локально, без Docker

```bash
python -m venv .venv && . .venv/Scripts/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
uvicorn app.main:app --reload
```

Для ML-пайплайна дополнительно: `pip install -e ".[ml]"`.

## Ключи внешних API

| Переменная | Обязателен | Где взять |
|---|---|---|
| `STEAM_API_KEY` | да, для live-контура | steamcommunity.com/dev/apikey |
| `OPENDOTA_API_KEY` | нет, повышает квоту | opendota.com |
| `STRATZ_API_TOKEN` | для быстрого бэкфилла | stratz.com |
| `LIQUIPEDIA_USER_AGENT` | да, при обращении к Liquipedia | указать название проекта и контактный email |

## Структура

Карта каталогов — в [CLAUDE.md](CLAUDE.md#карта-репозитория).

## Тесты и качество

```bash
pytest
ruff check . && ruff format --check .
mypy app
```

## Атрибуция

Данные турниров и разметка тиров — [Liquipedia](https://liquipedia.net/dota2), CC-BY-SA.
Игровые данные — Valve Corporation, OpenDota, STRATZ. Проект не аффилирован с Valve;
Dota 2 — торговая марка Valve Corporation. Сервис аналитический, к ставкам отношения не имеет.
