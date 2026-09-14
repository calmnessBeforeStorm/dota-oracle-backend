# Авторизация — бэкенд: план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Реализовать раздел 1 дизайна — пользователь из скрипта, вход по логину и паролю, access-токен в теле ответа, refresh-токен в httpOnly cookie с ротацией и детектом повторного использования, блокировка перебора, проверка прав по токену.

**Architecture:** Пакет `app/auth/` разделён по ответственности: `passwords` (Argon2id), `tokens` (JWT HS256), `permissions` (коды прав), `lockout` (счётчики неудач в Redis), `service` (вход, обновление, выход — вся логика с БД), `dependencies` (FastAPI-зависимости для Bearer и прав), `cli` (управление пользователем). HTTP-слой — тонкий `app/api/routes/auth.py`: он только переводит исключения сервиса в коды ответа и ставит или стирает cookie. Права проверяются по access-токену без запроса к базе.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 async + asyncpg, Alembic, redis-py asyncio, `argon2-cffi`, `PyJWT`, pytest (asyncio_mode=auto), httpx `ASGITransport`, ruff, mypy strict.

**Spec:** `docs/superpowers/specs/2026-09-11-auth-and-pipeline-panel-design.md`, раздел 1 и относящиеся к нему пункты раздела 4. Прочитать перед началом.

**Вне этого плана:** раздел 2 (реестр команд, `pipeline_runs`, задачи arq) и раздел 3 (фронтенд) — отдельные планы. Этот план даёт им `require_permission` и коды прав.

## Global Constraints

- Ветка `feature/auth` (создана от `development`, в неё уже влита ветка со спекой). В `main` не коммитить и не пушить.
- Все команды — из `dota-oracle-backend/`. Тесты гоняются из venv на хосте: `.venv/Scripts/python.exe -m pytest ...`; нужен запущенный `docker compose up -d postgres`. Тесты с БД без базы **пропускаются, а не падают — пропуск не считается прохождением**.
- Базовое состояние до начала: ruff и mypy чисты, **890 passed, 2 skipped**.
- Коммиты от локального `user.email` (`ersaimadilet@yandex.kz`, уже проставлен). Сообщения — английский, императив, со строчной буквы. **Никаких `Co-Authored-By` и прочих пометок об ИИ** — это запрещено `CLAUDE.md` проекта.
- Комментарии и докстринги — английский, только ASCII. Документация — русская.
- Значения из спеки, дословно: алгоритм `HS256`; access — **15 минут**; refresh — **7 дней**; cookie `refresh_token`, `HttpOnly`, `SameSite=Strict`, `Path=/api/auth`, `Max-Age=604800`, `Secure` только при `APP_ENV=production`; блокировка по логину — **5 неудач за 15 минут → 15 минут**, по IP — **20 неудач за 15 минут → 15 минут**; на блокировку `429` с `Retry-After` в секундах, пароль не проверяется; `SECRET_KEY` в production — не пустой и **не короче 32 байт**.
- Коды прав: `pipeline.runs.view`, `pipeline.run.<команда>`, `pipeline.run.chain`.
- Неверный логин и неверный пароль — **одинаковый** `401` с одним текстом; для несуществующего логина пароль всё равно проверяется против фиктивного хеша.
- Токены и пароли никогда не логируются. `RedactingFilter` о JWT ничего не знает — не передавать токен ни в одно событие лога.
- В CI нет Redis. Всё, что трогает Redis, тестируется на фейке.
- Синхронный `TestClient` не смешивать с фикстурой `sessionmaker`: у движка и у `TestClient` разные event loop, asyncpg упадёт. Тесты маршрутов с БД — только `httpx.AsyncClient` + `ASGITransport` (фикстура `api` из Task 5).
- Перед пушем — ровно то, что гоняет CI, без `.env`:
  `docker compose run --rm -e STRATZ_API_TOKEN= -e STEAM_API_KEY= tools python -m pytest` и
  `docker compose run --rm tools sh -c "ruff check . && ruff format --check . && mypy app"`.

## Решения, которых нет в спеке

Приняты здесь, чтобы не решать посреди задачи. Каждое записывается в спеку в Task 10.

| Вопрос | Решение | Почему |
|---|---|---|
| Откуда `create-user` знает «все права», если реестра команд (раздел 2) ещё нет | `app/auth/permissions.py` держит кортеж команд, тест сверяет его с подкомандами `app.ingestion.cli` (кроме `liquipedia`) | Раздел 2 потом заменит источник на реестр; до тех пор кнопка и терминал не разойдутся молча |
| `backfill-segments` в спеке не упомянута | Входит в список прав | Появилась 12.09, после спеки; это команда данных |
| Выход без cookie или с негодной cookie | `204` и очистка cookie | Выход идемпотентен; ошибка на выходе ничего не защищает |
| Минимальная длина пароля | 12 символов, проверяет CLI | У пользователя — права на запуск всех команд данных на проде |
| Две одновременных ротации одного refresh-токена | Ротация — условный `UPDATE ... WHERE token_hash = <предъявленный>`; если строка не обновилась, это считается повторным предъявлением | Иначе обе проверки хеша проходят гонкой и детектор слеп ровно в том случае, для которого существует |
| Проверка `SECRET_KEY` | Валидатор `Settings`, плюс отказ `deploy.sh` с понятным текстом | `alembic/env.py` читает `get_settings()`, поэтому без ключа упадёт уже контейнер миграций — лучше сказать это до него словами, а не трейсбэком pydantic |
| Значение ключа по умолчанию | Непустой dev-ключ; production отвергает и его | Иначе production без `SECRET_KEY` молча подписывал бы токены ключом из публичного репозитория, а пустое значение по умолчанию сломало бы локальный запуск и CI |

**Известный риск, поднять с владельцем (не решается в этом плане):** две вкладки, открытые одновременно (восстановление сессии браузера), вызовут `POST /refresh` с одной и той же cookie. Одна выиграет, вторая предъявит уже использованный токен — и детектор по спеке отзовёт сессии на всех устройствах. Single-flight из раздела 3 работает только внутри вкладки. Типовое решение — окно в несколько секунд, в котором принимается предыдущий хеш; это отступление от спеки и решение владельца.

---

## File Structure

| Файл | Ответственность |
|---|---|
| `pyproject.toml` (modify) | `argon2-cffi`, `PyJWT` в основных зависимостях |
| `app/core/config.py` (modify) | `secret_key`, `DEV_SECRET_KEY`, `MIN_SECRET_KEY_BYTES`, валидатор production |
| `.env.example`, `deploy/env.prod.example`, `deploy/deploy.sh` (modify) | `SECRET_KEY` в шаблонах и проверка при выкате |
| `app/auth/__init__.py` (create) | пакет |
| `app/auth/passwords.py` (create) | `hash_password`, `verify_password`, `dummy_hash` |
| `app/auth/tokens.py` (create) | выпуск и разбор access/refresh, `token_hash`, TTL |
| `app/auth/permissions.py` (create) | коды прав, `known_permissions()` |
| `app/ingestion/cli.py` (modify) | `build_parser()` вынесен из `main()` |
| `app/db/models/auth.py` (create), `app/db/models/__init__.py` (modify) | `User`, `AuthSession` |
| `alembic/versions/8b2e4f6a1c03_auth_tables.py` (create) | таблицы `users`, `auth_sessions` |
| `app/auth/lockout.py` (create) | счётчики неудач и блокировки в Redis |
| `app/auth/service.py` (create) | `login`, `refresh`, `logout`, `revoke_all_sessions` |
| `app/auth/dependencies.py` (create) | `current_claims`, `require_permission` |
| `app/schemas/auth.py` (create) | `LoginRequest`, `AuthUser`, `TokenResponse` |
| `app/api/routes/auth.py` (create), `app/api/router.py` (modify) | эндпоинты `/api/auth` |
| `Dockerfile.prod` (modify) | комментарий у `--forwarded-allow-ips` |
| `app/auth/cli.py` (create) | `create-user`, `set-password`, `revoke-sessions`, `grant-all` |
| `tests/auth/__init__.py`, `tests/auth/conftest.py` (create) | `FakeRedis`, фикстуры `fake_redis`, `make_user`, `api` |
| `tests/auth/test_*.py`, `tests/test_config.py` (create) | тесты |
| `CLAUDE.md`, спека (modify) | документация |

---

### Task 1: Зависимости и `SECRET_KEY`

**Files:**
- Modify: `pyproject.toml`
- Modify: `app/core/config.py`
- Modify: `.env.example`, `deploy/env.prod.example`, `deploy/deploy.sh`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `Settings.secret_key: str`; `app.core.config.DEV_SECRET_KEY: str`; `app.core.config.MIN_SECRET_KEY_BYTES = 32`.

- [ ] **Step 1: Добавить зависимости и установить**

В `pyproject.toml`, в `dependencies`, после `"lightgbm>=4.5",`:

```toml
    # Authentication (design 2026-09-11-auth-and-pipeline-panel, section 1). PyJWT used to
    # arrive only through redis; a direct import deserves a direct dependency.
    "argon2-cffi>=25.1",
    "PyJWT>=2.10",
```

Run: `.venv/Scripts/python.exe -m pip install -e ".[dev]"`
Run: `docker compose build tools worker`
Expected: обе команды завершаются без ошибок; `.venv/Scripts/python.exe -c "import argon2, jwt"` молчит.

- [ ] **Step 2: Написать падающий тест**

Create `tests/test_config.py`:

```python
"""Settings that must refuse to exist rather than run unsafely."""

import pytest
from pydantic import ValidationError

from app.core.config import DEV_SECRET_KEY, MIN_SECRET_KEY_BYTES, Settings


def test_local_runs_on_the_development_key() -> None:
    assert Settings(_env_file=None, app_env="local", secret_key=DEV_SECRET_KEY).secret_key


@pytest.mark.parametrize(
    "key",
    [
        "",
        "x" * (MIN_SECRET_KEY_BYTES - 1),
        # The default is in a public repository: anyone could mint tokens with it.
        DEV_SECRET_KEY,
        # 15 two-byte characters: 15 characters, 30 bytes. The limit is on bytes.
        "ж" * 15,
    ],
)
def test_production_refuses_an_unsafe_key(key: str) -> None:
    with pytest.raises(ValidationError, match="SECRET_KEY"):
        Settings(_env_file=None, app_env="production", secret_key=key)


def test_production_accepts_a_long_key() -> None:
    key = "x" * MIN_SECRET_KEY_BYTES
    assert Settings(_env_file=None, app_env="production", secret_key=key).secret_key == key


def test_the_refusal_does_not_print_the_key() -> None:
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None, app_env="production", secret_key="short-but-secret")
    assert "short-but-secret" not in str(caught.value)
```

- [ ] **Step 3: Убедиться, что тест падает**

Run: `.venv/Scripts/python.exe -m pytest tests/test_config.py -v`
Expected: FAIL — `ImportError: cannot import name 'DEV_SECRET_KEY'`.

- [ ] **Step 4: Реализовать**

В `app/core/config.py` заменить импорты typing и pydantic:

```python
from typing import Literal, Self

from pydantic import Field, model_validator
```

После импортов, перед `class Settings`:

```python
#: Signs tokens on a laptop and in CI, where nothing is worth protecting and nobody should need
#: setup to run the suite. Production refuses it: it is in a public repository.
DEV_SECRET_KEY = "dev-only-secret-key-refused-in-production"

#: HS256 wants a key at least as long as its 256-bit output.
MIN_SECRET_KEY_BYTES = 32
```

В `Settings`, после блока `# ML` (после `active_model_version`):

```python
    # Auth (design 2026-09-11-auth-and-pipeline-panel, section 1)
    secret_key: str = DEV_SECRET_KEY
```

В `Settings`, перед `@property def database_url`:

```python
    @model_validator(mode="after")
    def _production_needs_a_real_secret_key(self) -> Self:
        """Refuse to start rather than sign tokens with a key anybody can read.

        The message never includes the value: a startup failure is exactly what ends up pasted
        into an issue.
        """
        if self.app_env != "production":
            return self
        if self.secret_key == DEV_SECRET_KEY:
            raise ValueError("SECRET_KEY is the development default; set a real one in .env")
        if len(self.secret_key.encode()) < MIN_SECRET_KEY_BYTES:
            raise ValueError(
                f"SECRET_KEY must be at least {MIN_SECRET_KEY_BYTES} bytes in production; "
                "generate one with: openssl rand -base64 48"
            )
        return self
```

- [ ] **Step 5: Убедиться, что тест проходит**

Run: `.venv/Scripts/python.exe -m pytest tests/test_config.py -v`
Expected: PASS, 7 тестов.

- [ ] **Step 6: Шаблоны окружения и выкат**

В `.env.example`, после блока `# --- App ---` (после строки `CORS_ORIGINS=...`):

```bash

# --- Auth ---
# Signs login tokens. Leave it commented out locally: the built-in development key is used, and
# production refuses that key. Production needs its own: openssl rand -base64 48
# SECRET_KEY=
```

В `deploy/env.prod.example`, после блока `# --- database ---`:

```bash

# --- auth ---
# Signs login tokens. The API, the worker and the migration container all refuse to start
# without it: an empty or short key, or the development default, would mean tokens anyone can
# forge. At least 32 bytes. Generate, do not invent: openssl rand -base64 48
# Changing it logs everyone out.
SECRET_KEY=
```

В `deploy/deploy.sh`, сразу после проверки прав на `.env` (после строки `[ "$perms" = "600" ] || fail ...`):

```bash

# The migration container reads the same settings as the API, so a missing key would surface
# as a pydantic traceback halfway through the deploy. Said here, in words, before anything
# is pulled.
secret=$(grep -E '^SECRET_KEY=' "$ENV_FILE" | cut -d= -f2- | tr -d '"' || true)
[ "${#secret}" -ge 32 ] || fail "SECRET_KEY in $ENV_FILE is missing or shorter than 32 characters - run: openssl rand -base64 48"
```

- [ ] **Step 7: Полный прогон и коммит**

Run: `.venv/Scripts/python.exe -m pytest -p no:warnings -q` и `.venv/Scripts/ruff.exe check . && .venv/Scripts/ruff.exe format --check . && .venv/Scripts/mypy.exe app`
Expected: 897 passed, 2 skipped; ruff и mypy чисты.

```bash
git add pyproject.toml app/core/config.py tests/test_config.py .env.example deploy/env.prod.example deploy/deploy.sh
git commit -m "require a real secret key in production"
```

---

### Task 2: Пароли и токены

**Files:**
- Create: `app/auth/__init__.py`, `app/auth/passwords.py`, `app/auth/tokens.py`
- Test: `tests/auth/__init__.py`, `tests/auth/test_passwords.py`, `tests/auth/test_tokens.py`

**Interfaces:**
- Produces:
  - `hash_password(password: str) -> str`
  - `verify_password(password_hash: str, password: str) -> bool` — никогда не бросает
  - `dummy_hash() -> str`
  - `ALGORITHM = "HS256"`, `ACCESS_TTL = timedelta(minutes=15)`, `REFRESH_TTL = timedelta(days=7)`
  - `class InvalidTokenError(Exception)`
  - `@dataclass(frozen=True) AccessClaims(user_id: uuid.UUID, username: str, display_name: str, permissions: tuple[str, ...])`
  - `@dataclass(frozen=True) RefreshClaims(user_id: uuid.UUID, session_id: uuid.UUID)`
  - `issue_access_token(*, user_id: uuid.UUID, username: str, display_name: str, permissions: Sequence[str], secret: str, now: datetime) -> str`
  - `issue_refresh_token(*, user_id: uuid.UUID, session_id: uuid.UUID, secret: str, now: datetime) -> str`
  - `decode_access_token(token: str, secret: str) -> AccessClaims`
  - `decode_refresh_token(token: str, secret: str) -> RefreshClaims`
  - `token_hash(token: str) -> str` — SHA-256, hex

- [ ] **Step 1: Написать падающие тесты**

Create `tests/auth/__init__.py` пустым.

Create `tests/auth/test_passwords.py`:

```python
"""Password hashing (design 2026-09-11-auth-and-pipeline-panel, section 1)."""

from app.auth.passwords import dummy_hash, hash_password, verify_password


def test_hashes_are_argon2id() -> None:
    assert hash_password("correct horse battery staple").startswith("$argon2id$")


def test_the_same_password_hashes_differently_each_time() -> None:
    assert hash_password("correct horse battery staple") != hash_password(
        "correct horse battery staple"
    )


def test_the_right_password_verifies() -> None:
    stored = hash_password("correct horse battery staple")
    assert verify_password(stored, "correct horse battery staple") is True


def test_a_wrong_password_does_not_verify() -> None:
    assert verify_password(hash_password("correct horse battery staple"), "wrong") is False


def test_a_corrupt_hash_is_a_failed_check_not_a_crash() -> None:
    assert verify_password("not-a-hash", "anything") is False


def test_the_dummy_hash_is_a_real_hash_nobody_can_match() -> None:
    assert dummy_hash().startswith("$argon2id$")
    assert verify_password(dummy_hash(), "") is False
```

Create `tests/auth/test_tokens.py`:

```python
"""Access and refresh tokens (design 2026-09-11-auth-and-pipeline-panel, section 1)."""

import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.auth.tokens import (
    ACCESS_TTL,
    REFRESH_TTL,
    InvalidTokenError,
    decode_access_token,
    decode_refresh_token,
    issue_access_token,
    issue_refresh_token,
    token_hash,
)

SECRET = "test-secret-key-long-enough-for-hs256-signing"
USER_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
SESSION_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")


def _access(now: datetime | None = None, secret: str = SECRET) -> str:
    return issue_access_token(
        user_id=USER_ID,
        username="adilet",
        display_name="Adilet",
        permissions=["pipeline.runs.view"],
        secret=secret,
        now=now or datetime.now(UTC),
    )


def _refresh(now: datetime | None = None) -> str:
    return issue_refresh_token(
        user_id=USER_ID, session_id=SESSION_ID, secret=SECRET, now=now or datetime.now(UTC)
    )


def test_an_access_token_carries_the_user() -> None:
    claims = decode_access_token(_access(), SECRET)
    assert claims.user_id == USER_ID
    assert claims.username == "adilet"
    assert claims.display_name == "Adilet"
    assert claims.permissions == ("pipeline.runs.view",)


def test_a_refresh_token_carries_the_session() -> None:
    claims = decode_refresh_token(_refresh(), SECRET)
    assert claims.user_id == USER_ID
    assert claims.session_id == SESSION_ID


def test_lifetimes_follow_the_spec() -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    access = jwt.decode(_access(now), SECRET, algorithms=["HS256"])
    refresh = jwt.decode(_refresh(now), SECRET, algorithms=["HS256"])
    assert access["exp"] == int((now + ACCESS_TTL).timestamp())
    assert refresh["exp"] == int((now + REFRESH_TTL).timestamp())
    assert ACCESS_TTL == timedelta(minutes=15)
    assert REFRESH_TTL == timedelta(days=7)


def test_two_refresh_tokens_of_one_session_never_match() -> None:
    now = datetime.now(UTC)
    assert _refresh(now) != _refresh(now)


def test_an_expired_token_is_refused() -> None:
    long_ago = datetime.now(UTC) - REFRESH_TTL - timedelta(seconds=5)
    with pytest.raises(InvalidTokenError):
        decode_access_token(_access(long_ago), SECRET)
    with pytest.raises(InvalidTokenError):
        decode_refresh_token(_refresh(long_ago), SECRET)


def test_a_token_signed_with_another_key_is_refused() -> None:
    with pytest.raises(InvalidTokenError):
        decode_access_token(_access(secret="another-secret-key-long-enough-for-hs256"), SECRET)


def test_an_access_token_is_not_a_refresh_token() -> None:
    with pytest.raises(InvalidTokenError):
        decode_refresh_token(_access(), SECRET)


def test_a_refresh_token_is_not_an_access_token() -> None:
    with pytest.raises(InvalidTokenError):
        decode_access_token(_refresh(), SECRET)


def test_garbage_is_refused() -> None:
    with pytest.raises(InvalidTokenError):
        decode_access_token("not.a.token", SECRET)


def test_a_malformed_subject_is_refused() -> None:
    token = jwt.encode(
        {
            "sub": "not-a-uuid",
            "sid": str(SESSION_ID),
            "token_type": "refresh",
            "exp": datetime.now(UTC) + REFRESH_TTL,
        },
        SECRET,
        algorithm="HS256",
    )
    with pytest.raises(InvalidTokenError):
        decode_refresh_token(token, SECRET)


def test_the_stored_hash_is_sha256_hex() -> None:
    digest = token_hash("abc")
    assert digest == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `.venv/Scripts/python.exe -m pytest tests/auth -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.auth'`.

- [ ] **Step 3: Реализовать**

Create `app/auth/__init__.py`:

```python
"""Authentication (design 2026-09-11-auth-and-pipeline-panel, section 1)."""
```

Create `app/auth/passwords.py`:

```python
"""Password hashing with Argon2id."""

from functools import cache

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

#: argon2-cffi defaults to Argon2id with RFC 9106 parameters.
_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """True only for a match. A corrupt hash is a failed check, never an exception: a login
    endpoint that crashes on one row answers differently for that login, which is exactly the
    signal the uniform 401 exists to deny."""
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


@cache
def dummy_hash() -> str:
    """A real hash of a password nobody has, checked when the login does not exist.

    Returning 401 straight away for an unknown login would answer in microseconds, and a wrong
    password takes as long as Argon2 does - the timing alone would say which logins exist.
    Cached rather than computed at import so the CLI and the worker do not pay for it.
    """
    return _hasher.hash("no user has this password; it only exists to spend time")
```

Create `app/auth/tokens.py`:

```python
"""Signed access and refresh tokens.

Signed, not encrypted: anybody holding a token can read its claims, so nothing secret goes in.
"""

import hashlib
import secrets
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import jwt

ALGORITHM = "HS256"
ACCESS_TTL = timedelta(minutes=15)
REFRESH_TTL = timedelta(days=7)


class InvalidTokenError(Exception):
    """Bad signature, expired, malformed, or a token of the other type."""


@dataclass(frozen=True)
class AccessClaims:
    user_id: uuid.UUID
    username: str
    display_name: str
    permissions: tuple[str, ...]


@dataclass(frozen=True)
class RefreshClaims:
    user_id: uuid.UUID
    session_id: uuid.UUID


def issue_access_token(
    *,
    user_id: uuid.UUID,
    username: str,
    display_name: str,
    permissions: Sequence[str],
    secret: str,
    now: datetime,
) -> str:
    payload = {
        "sub": str(user_id),
        "username": username,
        "display_name": display_name,
        "permissions": list(permissions),
        "token_type": "access",
        "exp": now + ACCESS_TTL,
    }
    return jwt.encode(payload, secret, algorithm=ALGORITHM)


def issue_refresh_token(
    *, user_id: uuid.UUID, session_id: uuid.UUID, secret: str, now: datetime
) -> str:
    payload = {
        "sub": str(user_id),
        "sid": str(session_id),
        # Two tokens issued for one session in the same second would otherwise be identical,
        # and rotation would store the hash the client already presented.
        "jti": secrets.token_urlsafe(16),
        "token_type": "refresh",
        "exp": now + REFRESH_TTL,
    }
    return jwt.encode(payload, secret, algorithm=ALGORITHM)


def _decode(token: str, secret: str, token_type: str) -> dict[str, Any]:
    try:
        claims: dict[str, Any] = jwt.decode(
            token, secret, algorithms=[ALGORITHM], options={"require": ["exp", "sub"]}
        )
    except jwt.PyJWTError as exc:
        raise InvalidTokenError(type(exc).__name__) from exc
    # Without this a refresh token, which lives a week, would open every endpoint an access
    # token opens.
    if claims.get("token_type") != token_type:
        raise InvalidTokenError(f"expected a {token_type} token")
    return claims


def _uuid(value: object) -> uuid.UUID:
    if not isinstance(value, str):
        raise InvalidTokenError("malformed id")
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise InvalidTokenError("malformed id") from exc


def decode_access_token(token: str, secret: str) -> AccessClaims:
    claims = _decode(token, secret, "access")
    username = claims.get("username")
    display_name = claims.get("display_name")
    permissions = claims.get("permissions")
    if not isinstance(username, str) or not isinstance(display_name, str):
        raise InvalidTokenError("malformed user")
    if not isinstance(permissions, list) or not all(isinstance(p, str) for p in permissions):
        raise InvalidTokenError("malformed permissions")
    return AccessClaims(
        user_id=_uuid(claims["sub"]),
        username=username,
        display_name=display_name,
        permissions=tuple(permissions),
    )


def decode_refresh_token(token: str, secret: str) -> RefreshClaims:
    claims = _decode(token, secret, "refresh")
    return RefreshClaims(user_id=_uuid(claims["sub"]), session_id=_uuid(claims.get("sid")))


def token_hash(token: str) -> str:
    """What the database keeps instead of the token: a leaked row cannot be replayed."""
    return hashlib.sha256(token.encode()).hexdigest()
```

- [ ] **Step 4: Убедиться, что тесты проходят**

Run: `.venv/Scripts/python.exe -m pytest tests/auth -v`
Expected: PASS, 17 тестов.

- [ ] **Step 5: Линтеры и коммит**

Run: `.venv/Scripts/ruff.exe check . && .venv/Scripts/ruff.exe format --check . && .venv/Scripts/mypy.exe app`
Expected: чисто.

```bash
git add app/auth tests/auth
git commit -m "add password hashing and signed access and refresh tokens"
```

---

### Task 3: Коды прав

**Files:**
- Create: `app/auth/permissions.py`
- Modify: `app/ingestion/cli.py` (вынести построение парсера из `main()`)
- Test: `tests/auth/test_permissions.py`

**Interfaces:**
- Produces:
  - `RUNS_VIEW = "pipeline.runs.view"`, `RUN_CHAIN = "pipeline.run.chain"`
  - `PIPELINE_COMMANDS: tuple[str, ...]`
  - `run_permission(command: str) -> str`
  - `known_permissions() -> tuple[str, ...]`
  - `app.ingestion.cli.build_parser() -> argparse.ArgumentParser`

- [ ] **Step 1: Написать падающий тест**

Create `tests/auth/test_permissions.py`:

```python
"""Permission codes (design 2026-09-11-auth-and-pipeline-panel, section 1)."""

import argparse

from app.auth.permissions import (
    PIPELINE_COMMANDS,
    RUN_CHAIN,
    RUNS_VIEW,
    known_permissions,
    run_permission,
)
from app.ingestion.cli import build_parser

#: A debugging view of one Liquipedia page, not a data command (spec section 2).
NOT_PIPELINE_COMMANDS = {"liquipedia"}


def _cli_commands() -> set[str]:
    parser = build_parser()
    subparsers = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    return set(subparsers.choices)


def test_every_data_command_of_the_cli_has_a_permission() -> None:
    """A command added to the CLI and not here would have a button nobody can see."""
    assert set(PIPELINE_COMMANDS) == _cli_commands() - NOT_PIPELINE_COMMANDS


def test_a_run_permission_names_its_command() -> None:
    assert run_permission("backfill") == "pipeline.run.backfill"


def test_known_permissions_cover_viewing_the_chain_and_every_command() -> None:
    codes = known_permissions()
    assert codes[:2] == (RUNS_VIEW, RUN_CHAIN)
    assert set(codes[2:]) == {run_permission(c) for c in PIPELINE_COMMANDS}
    assert len(codes) == len(set(codes))
```

- [ ] **Step 2: Убедиться, что тест падает**

Run: `.venv/Scripts/python.exe -m pytest tests/auth/test_permissions.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.auth.permissions'`.

- [ ] **Step 3: Вынести `build_parser`**

В `app/ingestion/cli.py` функция `main()` начинается так:

```python
def main() -> None:
    parser = argparse.ArgumentParser(prog="app.ingestion.cli")
    sub = parser.add_subparsers(dest="command", required=True)
```

и заканчивает построение парсера строкой `sub.add_parser("status", help="show checkpoint and raw row counts")`, за которой идёт `args = parser.parse_args()`.

Превратить всё от `parser = argparse.ArgumentParser(...)` до `sub.add_parser("status", ...)` включительно в тело новой функции, не меняя в нём ни строки, и дописать `return parser`:

```python
def build_parser() -> argparse.ArgumentParser:
    """The command line, built without parsing anything, so tests can list its commands."""
    parser = argparse.ArgumentParser(prog="app.ingestion.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    # ... every add_parser / add_argument line exactly as it was in main() ...
    sub.add_parser("status", help="show checkpoint and raw row counts")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    configure_logging(get_settings().log_level)
    # ... the rest of main() unchanged ...
```

Run: `.venv/Scripts/python.exe -m app.ingestion.cli --help`
Expected: тот же список подкоманд, что и до правки.

- [ ] **Step 4: Реализовать коды прав**

Create `app/auth/permissions.py`:

```python
"""Permission codes carried in the access token.

Section 2 of the design introduces a command registry, and that registry becomes the source of
`PIPELINE_COMMANDS`. Until then the list lives here and a test holds it to the ingestion CLI, so
a command added there cannot silently lack a permission.
"""

RUNS_VIEW = "pipeline.runs.view"
RUN_CHAIN = "pipeline.run.chain"

PIPELINE_COMMANDS: tuple[str, ...] = (
    "reference",
    "backfill",
    "catch-up",
    "normalize",
    "details",
    "resolve-outcomes",
    "map-leagues",
    "refresh-meta",
    "refresh-stages",
    "link-stages",
    "prematch",
    "featurize",
    "backfill-segments",
    "status",
)


def run_permission(command: str) -> str:
    return f"pipeline.run.{command}"


def known_permissions() -> tuple[str, ...]:
    """Every code that exists today - what `create-user` and `grant-all` hand out."""
    return (RUNS_VIEW, RUN_CHAIN, *(run_permission(c) for c in PIPELINE_COMMANDS))
```

- [ ] **Step 5: Убедиться, что тест проходит**

Run: `.venv/Scripts/python.exe -m pytest tests/auth/test_permissions.py -v`
Expected: PASS, 3 теста.

- [ ] **Step 6: Линтеры и коммит**

Run: `.venv/Scripts/ruff.exe check . && .venv/Scripts/ruff.exe format --check . && .venv/Scripts/mypy.exe app`
Expected: чисто.

```bash
git add app/auth/permissions.py app/ingestion/cli.py tests/auth/test_permissions.py
git commit -m "add permission codes held to the ingestion cli"
```

---

### Task 4: Таблицы `users` и `auth_sessions`

**Files:**
- Create: `app/db/models/auth.py`
- Modify: `app/db/models/__init__.py`
- Create: `alembic/versions/8b2e4f6a1c03_auth_tables.py`
- Test: `tests/auth/test_models.py`

**Interfaces:**
- Produces:
  - `User(Base, TimestampMixin)`: `id: uuid.UUID` (по умолчанию `uuid4`), `username: str` (unique), `display_name: str`, `password_hash: str`, `permissions: list[str]`, `is_active: bool` (по умолчанию `True`)
  - `AuthSession(Base)`: `id: uuid.UUID` (по умолчанию `uuid4`), `user_id: uuid.UUID`, `token_hash: str`, `created_at: datetime`, `last_used_at: datetime`, `expires_at: datetime`, `revoked_at: datetime | None`

- [ ] **Step 1: Написать падающий тест**

Create `tests/auth/test_models.py`:

```python
"""The auth tables hold what section 1 says and cascade the way it says."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.auth import AuthSession, User


def _user(username: str = "adilet") -> User:
    return User(
        username=username,
        display_name="Adilet",
        password_hash="$argon2id$placeholder",
        permissions=["pipeline.runs.view"],
    )


async def test_a_user_round_trips(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    async with sessionmaker() as session:
        user = _user()
        session.add(user)
        await session.commit()

    async with sessionmaker() as session:
        stored = await session.scalar(select(User).where(User.username == "adilet"))
    assert stored is not None
    assert isinstance(stored.id, uuid.UUID)
    assert stored.permissions == ["pipeline.runs.view"]
    assert stored.is_active is True
    assert stored.created_at.tzinfo is not None


async def test_usernames_are_unique(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    async with sessionmaker() as session:
        session.add(_user())
        await session.commit()
    async with sessionmaker() as session:
        session.add(_user())
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_deleting_a_user_deletes_their_sessions(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    async with sessionmaker() as session:
        user = _user()
        session.add(user)
        await session.flush()
        session.add(
            AuthSession(user_id=user.id, token_hash="h", expires_at=now + timedelta(days=7))
        )
        await session.commit()
        await session.execute(delete(User).where(User.id == user.id))
        await session.commit()
        assert await session.scalar(select(AuthSession)) is None
```

- [ ] **Step 2: Убедиться, что тест падает**

Run: `.venv/Scripts/python.exe -m pytest tests/auth/test_models.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.db.models.auth'`.

- [ ] **Step 3: Реализовать модели**

Create `app/db/models/auth.py`:

```python
"""Users and their sessions (design 2026-09-11-auth-and-pipeline-panel, section 1)."""

import uuid
from datetime import datetime

from sqlalchemy import ARRAY, Boolean, DateTime, ForeignKey, Text, Uuid, func, true
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    #: Argon2id, never the password.
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    #: Codes from `app.auth.permissions`. Copied into the access token at issue, so a change
    #: here reaches a signed-in user within one access-token lifetime, not instantly.
    permissions: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )


class AuthSession(Base):
    """One row per signed-in device.

    Holds the hash of the *current* refresh token only. A valid signature on a token whose hash
    is not this one means an already-rotated token came back - which is what theft looks like.
    """

    __tablename__ = "auth_sessions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_used_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
```

В `app/db/models/__init__.py` добавить импорт перед `from app.db.models.enums ...`:

```python
from app.db.models.auth import AuthSession, User
```

и в `__all__` — `"AuthSession"` первой строкой списка и `"User"` между `"TournamentStage"` и концом списка (алфавитный порядок).

- [ ] **Step 4: Написать миграцию**

Create `alembic/versions/8b2e4f6a1c03_auth_tables.py`:

```python
"""auth tables

Revision ID: 8b2e4f6a1c03
Revises: 3f8a1c2d9e41
Create Date: 2026-09-14 12:00:00.000000

Users and their sessions. The first user is created by `python -m app.auth.cli create-user`,
never by a migration: a password in a migration is a password in git.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "8b2e4f6a1c03"
down_revision: str | None = "3f8a1c2d9e41"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("permissions", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("username", name=op.f("uq_users_username")),
    )
    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_used_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_auth_sessions_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_sessions")),
    )
    op.create_index(op.f("ix_auth_sessions_user_id"), "auth_sessions", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_auth_sessions_user_id"), table_name="auth_sessions")
    op.drop_table("auth_sessions")
    op.drop_table("users")
```

- [ ] **Step 5: Убедиться, что тесты проходят**

Run: `.venv/Scripts/python.exe -m pytest tests/auth/test_models.py tests/test_migrations.py -v`
Expected: PASS — 3 теста моделей и все тесты миграций (один head, обе таблицы созданы миграцией).

- [ ] **Step 6: Прогнать миграцию на локальной базе туда и обратно на одну ревизию**

**Не `downgrade base`** — он дропает накопленное сырьё. `-1` снимает только эту миграцию.

Run: `docker compose run --rm tools alembic upgrade head`
Run: `docker compose run --rm tools alembic downgrade -1`
Run: `docker compose run --rm tools alembic upgrade head`
Expected: три прогона без ошибок; после последнего `docker compose exec postgres psql -U dota -d dota_oracle -c "\d auth_sessions"` показывает `fk_auth_sessions_user_id_users ... ON DELETE CASCADE`.

- [ ] **Step 7: Линтеры и коммит**

Run: `.venv/Scripts/ruff.exe check . && .venv/Scripts/ruff.exe format --check . && .venv/Scripts/mypy.exe app`
Expected: чисто.

```bash
git add app/db/models/auth.py app/db/models/__init__.py alembic/versions/8b2e4f6a1c03_auth_tables.py tests/auth/test_models.py
git commit -m "add users and auth sessions tables"
```

---

### Task 5: Блокировка перебора и тестовая инфраструктура

**Files:**
- Create: `app/auth/lockout.py`
- Create: `tests/auth/conftest.py`
- Test: `tests/auth/test_lockout.py`

**Interfaces:**
- Consumes: `User` (Task 4), `hash_password` (Task 2), `RUNS_VIEW` (Task 3).
- Produces:
  - `LOGIN_LIMIT = 5`, `IP_LIMIT = 20`, `WINDOW_SECONDS = 900`, `LOCK_SECONDS = 900`
  - `failure_key(kind: str, value: str) -> str`, `lock_key(kind: str, value: str) -> str` — `kind` это `"login"` или `"ip"`
  - `async retry_after(redis: Redis, *, username: str, ip: str) -> int | None`
  - `async record_failure(redis: Redis, *, username: str, ip: str) -> None`
  - `async reset_login(redis: Redis, *, username: str) -> None`
  - фикстуры тестов: `fake_redis -> FakeRedis`, `make_user -> async (username="adilet", password=PASSWORD, permissions=(RUNS_VIEW,), is_active=True) -> User`, `api -> httpx.AsyncClient`; константы `PASSWORD`, `SECRET` в `tests/auth/conftest.py`

- [ ] **Step 1: Тестовая инфраструктура**

Create `tests/auth/conftest.py`:

```python
"""Fixtures shared by the auth tests.

Route tests use `httpx.AsyncClient` over `ASGITransport`, never the sync `TestClient`: the
`sessionmaker` fixture builds its engine in pytest's event loop, and `TestClient` runs the app in
a loop of its own, where asyncpg refuses a connection that belongs to another loop.
"""

from collections.abc import AsyncIterator, Awaitable, Callable, Sequence

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.passwords import hash_password
from app.auth.permissions import RUNS_VIEW
from app.core.redis import get_redis
from app.db.models.auth import User
from app.db.session import get_session
from app.main import create_app

PASSWORD = "correct horse battery staple"
SECRET = "test-secret-key-long-enough-for-hs256-signing"


class FakeRedis:
    """The handful of commands auth uses. TTLs are recorded, not counted down."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    async def incr(self, name: str) -> int:
        value = int(self.values.get(name, "0")) + 1
        self.values[name] = str(value)
        return value

    async def expire(self, name: str, time: int, nx: bool = False) -> bool:
        if name not in self.values or (nx and name in self.ttls):
            return False
        self.ttls[name] = time
        return True

    async def ttl(self, name: str) -> int:
        if name not in self.values:
            return -2
        return self.ttls.get(name, -1)

    async def set(
        self, name: str, value: str, ex: int | None = None, nx: bool = False
    ) -> bool | None:
        if nx and name in self.values:
            return None
        self.values[name] = value
        if ex is None:
            self.ttls.pop(name, None)
        else:
            self.ttls[name] = ex
        return True

    async def delete(self, *names: str) -> int:
        removed = 0
        for name in names:
            if name in self.values:
                removed += 1
            self.values.pop(name, None)
            self.ttls.pop(name, None)
        return removed


@pytest.fixture
def fake_redis() -> FakeRedis:
    return FakeRedis()


MakeUser = Callable[..., Awaitable[User]]


@pytest.fixture
def make_user(sessionmaker: async_sessionmaker[AsyncSession]) -> MakeUser:
    async def _make(
        username: str = "adilet",
        password: str = PASSWORD,
        permissions: Sequence[str] = (RUNS_VIEW,),
        is_active: bool = True,
    ) -> User:
        async with sessionmaker() as session:
            user = User(
                username=username,
                display_name="Adilet",
                password_hash=hash_password(password),
                permissions=list(permissions),
                is_active=is_active,
            )
            session.add(user)
            await session.commit()
            return user

    return _make


@pytest.fixture
async def api(
    sessionmaker: async_sessionmaker[AsyncSession], fake_redis: FakeRedis
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()

    async def _session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_redis] = lambda: fake_redis
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
```

- [ ] **Step 2: Написать падающий тест**

Create `tests/auth/test_lockout.py`:

```python
"""Login lockout (design 2026-09-11-auth-and-pipeline-panel, section 1)."""

from app.auth.lockout import (
    IP_LIMIT,
    LOCK_SECONDS,
    LOGIN_LIMIT,
    WINDOW_SECONDS,
    failure_key,
    record_failure,
    reset_login,
    retry_after,
)
from tests.auth.conftest import FakeRedis


def test_limits_follow_the_spec() -> None:
    assert (LOGIN_LIMIT, IP_LIMIT) == (5, 20)
    assert WINDOW_SECONDS == LOCK_SECONDS == 15 * 60


async def test_nothing_is_locked_at_first(fake_redis: FakeRedis) -> None:
    assert await retry_after(fake_redis, username="adilet", ip="10.0.0.1") is None


async def test_one_failure_short_of_the_limit_does_not_lock(fake_redis: FakeRedis) -> None:
    for i in range(LOGIN_LIMIT - 1):
        await record_failure(fake_redis, username="adilet", ip=f"10.0.0.{i}")
    assert await retry_after(fake_redis, username="adilet", ip="10.0.0.99") is None


async def test_the_login_limit_locks_that_login_from_any_address(fake_redis: FakeRedis) -> None:
    for i in range(LOGIN_LIMIT):
        await record_failure(fake_redis, username="adilet", ip=f"10.0.0.{i}")
    assert await retry_after(fake_redis, username="adilet", ip="10.0.0.99") == LOCK_SECONDS
    assert await retry_after(fake_redis, username="someone", ip="10.0.0.99") is None


async def test_the_ip_limit_locks_every_login_from_that_address(fake_redis: FakeRedis) -> None:
    for i in range(IP_LIMIT):
        await record_failure(fake_redis, username=f"guess-{i}", ip="10.0.0.1")
    assert await retry_after(fake_redis, username="anyone", ip="10.0.0.1") == LOCK_SECONDS
    assert await retry_after(fake_redis, username="anyone", ip="10.0.0.2") is None


async def test_the_window_starts_at_the_first_failure(fake_redis: FakeRedis) -> None:
    """A later failure must not push the window out, or slow guessing never resets."""
    await record_failure(fake_redis, username="adilet", ip="10.0.0.1")
    key = failure_key("login", "adilet")
    assert fake_redis.ttls[key] == WINDOW_SECONDS
    fake_redis.ttls[key] = 100
    await record_failure(fake_redis, username="adilet", ip="10.0.0.1")
    assert fake_redis.ttls[key] == 100


async def test_a_counter_left_without_a_window_gets_one(fake_redis: FakeRedis) -> None:
    """A crash between INCR and EXPIRE would otherwise leave a counter that never expires."""
    key = failure_key("login", "adilet")
    fake_redis.values[key] = "2"
    await record_failure(fake_redis, username="adilet", ip="10.0.0.1")
    assert fake_redis.ttls[key] == WINDOW_SECONDS


async def test_reset_clears_the_login_counter_but_not_the_address(fake_redis: FakeRedis) -> None:
    await record_failure(fake_redis, username="adilet", ip="10.0.0.1")
    await reset_login(fake_redis, username="adilet")
    assert failure_key("login", "adilet") not in fake_redis.values
    assert fake_redis.values[failure_key("ip", "10.0.0.1")] == "1"
```

- [ ] **Step 3: Убедиться, что тест падает**

Run: `.venv/Scripts/python.exe -m pytest tests/auth/test_lockout.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.auth.lockout'`.

- [ ] **Step 4: Реализовать**

Create `app/auth/lockout.py`:

```python
"""Failed-login counters and locks in Redis.

Two counters per attempt: by login, which stops guessing one account from many addresses, and by
address, which stops guessing many accounts from one. The address is whatever uvicorn put in
`request.client` - see the comment on `--forwarded-allow-ips` in Dockerfile.prod for why that is
only trustworthy while the API port stays unpublished.
"""

from redis.asyncio import Redis

LOGIN_LIMIT = 5
IP_LIMIT = 20
WINDOW_SECONDS = 15 * 60
LOCK_SECONDS = 15 * 60


def failure_key(kind: str, value: str) -> str:
    return f"auth:failures:{kind}:{value}"


def lock_key(kind: str, value: str) -> str:
    return f"auth:lock:{kind}:{value}"


async def retry_after(redis: Redis, *, username: str, ip: str) -> int | None:
    """Seconds until the longest active lock ends, or None when neither is locked."""
    # int(): redis-py types command results as Any, and mypy strict refuses to return Any.
    remaining = [
        int(await redis.ttl(lock_key("login", username))),
        int(await redis.ttl(lock_key("ip", ip))),
    ]
    # TTL answers -2 for a missing key and -1 for one without expiry; neither is a lock here.
    active = [seconds for seconds in remaining if seconds > 0]
    return max(active) if active else None


async def record_failure(redis: Redis, *, username: str, ip: str) -> None:
    for kind, value, limit in (("login", username, LOGIN_LIMIT), ("ip", ip, IP_LIMIT)):
        key = failure_key(kind, value)
        count = await redis.incr(key)
        # NX: the window is fixed at the first failure, and a counter that lost its expiry to a
        # crash between the two calls gets one back on the next failure instead of never.
        await redis.expire(key, WINDOW_SECONDS, nx=True)
        if count >= limit:
            await redis.set(lock_key(kind, value), "1", ex=LOCK_SECONDS)
            await redis.delete(key)


async def reset_login(redis: Redis, *, username: str) -> None:
    """After a successful login. The address counter stays: one good password from an address
    that has been guessing other accounts says nothing about those guesses."""
    await redis.delete(failure_key("login", username))
```

- [ ] **Step 5: Убедиться, что тест проходит**

Run: `.venv/Scripts/python.exe -m pytest tests/auth/test_lockout.py -v`
Expected: PASS, 8 тестов.

- [ ] **Step 6: Линтеры и коммит**

Run: `.venv/Scripts/ruff.exe check . && .venv/Scripts/ruff.exe format --check . && .venv/Scripts/mypy.exe app`
Expected: чисто.

```bash
git add app/auth/lockout.py tests/auth/conftest.py tests/auth/test_lockout.py
git commit -m "lock logins after repeated failures per login and per address"
```

---

### Task 6: Сервис — вход, обновление, выход

**Files:**
- Create: `app/auth/service.py`
- Test: `tests/auth/test_service.py`

**Interfaces:**
- Consumes: всё из Tasks 2–5.
- Produces:
  - `@dataclass(frozen=True) IssuedTokens(access_token: str, refresh_token: str, user: User)`
  - `class InvalidCredentialsError(Exception)`
  - `class LoginLockedError(Exception)` с атрибутом `retry_after: int`
  - `class RefreshRejectedError(Exception)`
  - `async login(session: AsyncSession, redis: Redis, *, username: str, password: str, ip: str, secret: str) -> IssuedTokens`
  - `async refresh(session: AsyncSession, *, refresh_token: str, ip: str, secret: str) -> IssuedTokens`
  - `async logout(session: AsyncSession, *, refresh_token: str | None, secret: str) -> None`
  - `async revoke_all_sessions(session: AsyncSession, user_id: uuid.UUID) -> int` — без commit

- [ ] **Step 1: Написать падающий тест**

Create `tests/auth/test_service.py`:

```python
"""Login, rotation and theft detection (design 2026-09-11-auth-and-pipeline-panel, section 1).

Each call gets its own session, the way each request does: one shared session would answer
from its identity map and hide what a concurrent request actually sees.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth import service
from app.auth.lockout import LOCK_SECONDS, failure_key, lock_key
from app.auth.service import (
    InvalidCredentialsError,
    IssuedTokens,
    LoginLockedError,
    RefreshRejectedError,
)
from app.auth.tokens import decode_access_token, decode_refresh_token, token_hash
from app.db.models.auth import AuthSession, User
from tests.auth.conftest import PASSWORD, SECRET, FakeRedis, MakeUser

Sessions = async_sessionmaker[AsyncSession]
IP = "10.0.0.1"


class _Recorder:
    """Stands in for the module logger. Not structlog's capture_logs: that one misses loggers
    cached before it was entered, and any test that runs the app lifespan first turns caching on."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def info(self, event: str, **fields: Any) -> None:
        self.events.append({"event": event, **fields})

    warning = info


@pytest.fixture
def logs(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    recorder = _Recorder()
    monkeypatch.setattr(service, "log", recorder)
    return recorder.events


async def _login(sessions: Sessions, redis: FakeRedis, password: str = PASSWORD) -> IssuedTokens:
    async with sessions() as session:
        return await service.login(
            session, redis, username="adilet", password=password, ip=IP, secret=SECRET
        )


async def _refresh(sessions: Sessions, token: str) -> IssuedTokens:
    async with sessions() as session:
        return await service.refresh(session, refresh_token=token, ip=IP, secret=SECRET)


async def _rows(sessions: Sessions) -> list[AuthSession]:
    async with sessions() as session:
        return list(await session.scalars(select(AuthSession)))


def _spy_on_verify(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    checked: list[str] = []
    real: Callable[[str, str], bool] = service.verify_password

    def spy(password_hash: str, password: str) -> bool:
        checked.append(password_hash)
        return real(password_hash, password)

    monkeypatch.setattr(service, "verify_password", spy)
    return checked


class TestLogin:
    async def test_issues_tokens_and_records_the_session(
        self, sessionmaker: Sessions, fake_redis: FakeRedis, make_user: MakeUser
    ) -> None:
        user = await make_user()
        issued = await _login(sessionmaker, fake_redis)

        access = decode_access_token(issued.access_token, SECRET)
        assert access.user_id == user.id
        assert access.permissions == tuple(user.permissions)

        claims = decode_refresh_token(issued.refresh_token, SECRET)
        (row,) = await _rows(sessionmaker)
        assert row.id == claims.session_id
        assert row.user_id == user.id
        assert row.token_hash == token_hash(issued.refresh_token)
        assert row.revoked_at is None
        assert row.expires_at > datetime.now(UTC) + timedelta(days=6)

    async def test_a_wrong_password_is_refused_and_counted(
        self, sessionmaker: Sessions, fake_redis: FakeRedis, make_user: MakeUser
    ) -> None:
        await make_user()
        with pytest.raises(InvalidCredentialsError):
            await _login(sessionmaker, fake_redis, password="wrong")
        assert fake_redis.values[failure_key("login", "adilet")] == "1"
        assert await _rows(sessionmaker) == []

    async def test_an_unknown_login_still_spends_a_hash_check(
        self, sessionmaker: Sessions, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        checked = _spy_on_verify(monkeypatch)
        with pytest.raises(InvalidCredentialsError):
            await _login(sessionmaker, fake_redis)
        assert checked == [service.dummy_hash()]

    async def test_an_inactive_user_cannot_log_in(
        self, sessionmaker: Sessions, fake_redis: FakeRedis, make_user: MakeUser
    ) -> None:
        await make_user(is_active=False)
        with pytest.raises(InvalidCredentialsError):
            await _login(sessionmaker, fake_redis)

    async def test_a_locked_login_is_refused_without_checking_the_password(
        self,
        sessionmaker: Sessions,
        fake_redis: FakeRedis,
        make_user: MakeUser,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await make_user()
        await fake_redis.set(lock_key("login", "adilet"), "1", ex=LOCK_SECONDS)
        checked = _spy_on_verify(monkeypatch)
        with pytest.raises(LoginLockedError) as caught:
            await _login(sessionmaker, fake_redis)
        assert caught.value.retry_after == LOCK_SECONDS
        assert checked == []

    async def test_success_resets_the_login_counter(
        self, sessionmaker: Sessions, fake_redis: FakeRedis, make_user: MakeUser
    ) -> None:
        await make_user()
        with pytest.raises(InvalidCredentialsError):
            await _login(sessionmaker, fake_redis, password="wrong")
        await _login(sessionmaker, fake_redis)
        assert failure_key("login", "adilet") not in fake_redis.values

    async def test_the_password_is_never_logged(
        self,
        sessionmaker: Sessions,
        fake_redis: FakeRedis,
        make_user: MakeUser,
        logs: list[dict[str, Any]],
    ) -> None:
        await make_user()
        issued = await _login(sessionmaker, fake_redis)
        with pytest.raises(InvalidCredentialsError):
            await _login(sessionmaker, fake_redis, password="wrong-password-value")
        rendered = repr(logs)
        assert PASSWORD not in rendered
        assert "wrong-password-value" not in rendered
        assert issued.refresh_token not in rendered
        assert issued.access_token not in rendered
        assert {"auth.login_succeeded", "auth.login_failed"} <= {e["event"] for e in logs}


class TestRefresh:
    async def test_rotates_the_token(
        self, sessionmaker: Sessions, fake_redis: FakeRedis, make_user: MakeUser
    ) -> None:
        await make_user()
        first = await _login(sessionmaker, fake_redis)
        second = await _refresh(sessionmaker, first.refresh_token)

        assert second.refresh_token != first.refresh_token
        (row,) = await _rows(sessionmaker)
        assert row.token_hash == token_hash(second.refresh_token)
        assert decode_refresh_token(second.refresh_token, SECRET).session_id == row.id

    async def test_reusing_a_rotated_token_revokes_every_session_of_the_user(
        self,
        sessionmaker: Sessions,
        fake_redis: FakeRedis,
        make_user: MakeUser,
        logs: list[dict[str, Any]],
    ) -> None:
        await make_user()
        laptop = await _login(sessionmaker, fake_redis)
        await _login(sessionmaker, fake_redis)  # a second device
        await _refresh(sessionmaker, laptop.refresh_token)

        with pytest.raises(RefreshRejectedError):
            await _refresh(sessionmaker, laptop.refresh_token)

        rows = await _rows(sessionmaker)
        assert len(rows) == 2
        assert all(row.revoked_at is not None for row in rows)
        (event,) = [e for e in logs if e["event"] == "auth.refresh_reuse"]
        assert event["ip"] == IP
        assert laptop.refresh_token not in repr(logs)

    async def test_a_revoked_session_cannot_refresh(
        self, sessionmaker: Sessions, fake_redis: FakeRedis, make_user: MakeUser
    ) -> None:
        await make_user()
        issued = await _login(sessionmaker, fake_redis)
        async with sessionmaker() as session:
            await session.execute(update(AuthSession).values(revoked_at=datetime.now(UTC)))
            await session.commit()
        with pytest.raises(RefreshRejectedError):
            await _refresh(sessionmaker, issued.refresh_token)

    async def test_an_expired_session_cannot_refresh(
        self, sessionmaker: Sessions, fake_redis: FakeRedis, make_user: MakeUser
    ) -> None:
        await make_user()
        issued = await _login(sessionmaker, fake_redis)
        async with sessionmaker() as session:
            await session.execute(
                update(AuthSession).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
            await session.commit()
        with pytest.raises(RefreshRejectedError):
            await _refresh(sessionmaker, issued.refresh_token)

    async def test_a_deactivated_user_cannot_refresh(
        self, sessionmaker: Sessions, fake_redis: FakeRedis, make_user: MakeUser
    ) -> None:
        user = await make_user()
        issued = await _login(sessionmaker, fake_redis)
        async with sessionmaker() as session:
            stored = await session.get(User, user.id)
            assert stored is not None
            stored.is_active = False
            await session.commit()
        with pytest.raises(RefreshRejectedError):
            await _refresh(sessionmaker, issued.refresh_token)

    async def test_an_access_token_cannot_refresh(
        self, sessionmaker: Sessions, fake_redis: FakeRedis, make_user: MakeUser
    ) -> None:
        await make_user()
        issued = await _login(sessionmaker, fake_redis)
        with pytest.raises(RefreshRejectedError):
            await _refresh(sessionmaker, issued.access_token)


class TestLogout:
    async def test_revokes_the_session(
        self, sessionmaker: Sessions, fake_redis: FakeRedis, make_user: MakeUser
    ) -> None:
        await make_user()
        issued = await _login(sessionmaker, fake_redis)
        async with sessionmaker() as session:
            await service.logout(session, refresh_token=issued.refresh_token, secret=SECRET)
        (row,) = await _rows(sessionmaker)
        assert row.revoked_at is not None
        with pytest.raises(RefreshRejectedError):
            await _refresh(sessionmaker, issued.refresh_token)

    @pytest.mark.parametrize("token", [None, "", "garbage"])
    async def test_without_a_usable_token_is_a_no_op(
        self, sessionmaker: Sessions, token: str | None
    ) -> None:
        async with sessionmaker() as session:
            await service.logout(session, refresh_token=token, secret=SECRET)
```

- [ ] **Step 2: Убедиться, что тест падает**

Run: `.venv/Scripts/python.exe -m pytest tests/auth/test_service.py -v`
Expected: FAIL — `ImportError: cannot import name 'service' from 'app.auth'`.

- [ ] **Step 3: Реализовать**

Create `app/auth/service.py`:

```python
"""Login, refresh and logout (design 2026-09-11-auth-and-pipeline-panel, section 1).

Everything that touches the database lives here; the routes only translate these exceptions into
status codes and set or clear the cookie. Nothing in this module logs a token or a password.
"""

import asyncio
import hmac
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import lockout
from app.auth.passwords import dummy_hash, verify_password
from app.auth.tokens import (
    REFRESH_TTL,
    InvalidTokenError,
    decode_refresh_token,
    issue_access_token,
    issue_refresh_token,
    token_hash,
)
from app.core.logging import get_logger
from app.db.models.auth import AuthSession, User

log = get_logger(__name__)


class InvalidCredentialsError(Exception):
    """Unknown login, wrong password or inactive user - deliberately indistinguishable."""


class LoginLockedError(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__(f"locked for {retry_after}s")
        self.retry_after = retry_after


class RefreshRejectedError(Exception):
    """The refresh token does not open a live session. The cookie should be cleared."""


@dataclass(frozen=True)
class IssuedTokens:
    access_token: str
    refresh_token: str
    user: User


def _now() -> datetime:
    return datetime.now(UTC)


def _access_token(user: User, secret: str, now: datetime) -> str:
    return issue_access_token(
        user_id=user.id,
        username=user.username,
        display_name=user.display_name,
        permissions=user.permissions,
        secret=secret,
        now=now,
    )


async def login(
    session: AsyncSession,
    redis: Redis,
    *,
    username: str,
    password: str,
    ip: str,
    secret: str,
) -> IssuedTokens:
    wait = await lockout.retry_after(redis, username=username, ip=ip)
    if wait is not None:
        log.info("auth.login_locked", username=username, ip=ip, retry_after=wait)
        raise LoginLockedError(wait)

    user = await session.scalar(select(User).where(User.username == username))
    # Argon2 is CPU work measured in tens of milliseconds; off the event loop, or every other
    # request waits for it.
    stored = user.password_hash if user is not None else dummy_hash()
    matches = await asyncio.to_thread(verify_password, stored, password)
    if user is None or not user.is_active or not matches:
        await lockout.record_failure(redis, username=username, ip=ip)
        log.info("auth.login_failed", username=username, ip=ip)
        raise InvalidCredentialsError

    await lockout.reset_login(redis, username=username)
    now = _now()
    row = AuthSession(
        id=uuid.uuid4(),
        user_id=user.id,
        token_hash="",
        created_at=now,
        last_used_at=now,
        expires_at=now + REFRESH_TTL,
    )
    refresh_token = issue_refresh_token(user_id=user.id, session_id=row.id, secret=secret, now=now)
    row.token_hash = token_hash(refresh_token)
    session.add(row)
    await session.commit()
    log.info("auth.login_succeeded", username=username, ip=ip, session_id=str(row.id))
    return IssuedTokens(_access_token(user, secret, now), refresh_token, user)


async def revoke_all_sessions(session: AsyncSession, user_id: uuid.UUID) -> int:
    """Revoke every live session of a user. The caller commits."""
    result = await session.execute(
        update(AuthSession)
        .where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=_now())
    )
    return int(result.rowcount or 0)


async def _reject_reuse(session: AsyncSession, user_id: uuid.UUID, ip: str) -> None:
    revoked = await revoke_all_sessions(session, user_id)
    await session.commit()
    log.warning("auth.refresh_reuse", user_id=str(user_id), ip=ip, revoked_sessions=revoked)


async def refresh(
    session: AsyncSession, *, refresh_token: str, ip: str, secret: str
) -> IssuedTokens:
    try:
        claims = decode_refresh_token(refresh_token, secret)
    except InvalidTokenError:
        raise RefreshRejectedError from None

    now = _now()
    row = await session.get(AuthSession, claims.session_id, populate_existing=True)
    if (
        row is None
        or row.user_id != claims.user_id
        or row.revoked_at is not None
        or row.expires_at <= now
    ):
        raise RefreshRejectedError

    presented = token_hash(refresh_token)
    if not hmac.compare_digest(row.token_hash, presented):
        await _reject_reuse(session, row.user_id, ip)
        raise RefreshRejectedError

    user = await session.get(User, row.user_id, populate_existing=True)
    if user is None or not user.is_active:
        raise RefreshRejectedError

    new_token = issue_refresh_token(user_id=user.id, session_id=row.id, secret=secret, now=now)
    # Conditional on the hash just checked: two requests presenting the same token both pass the
    # comparison above, and without this condition both would rotate. The loser is presenting a
    # token that is no longer current, which is the reuse case by definition.
    rotated = await session.execute(
        update(AuthSession)
        .where(
            AuthSession.id == row.id,
            AuthSession.token_hash == presented,
            AuthSession.revoked_at.is_(None),
        )
        .values(token_hash=token_hash(new_token), last_used_at=now)
    )
    if int(rotated.rowcount or 0) != 1:
        await session.rollback()
        await _reject_reuse(session, row.user_id, ip)
        raise RefreshRejectedError

    await session.commit()
    return IssuedTokens(_access_token(user, secret, now), new_token, user)


async def logout(session: AsyncSession, *, refresh_token: str | None, secret: str) -> None:
    """Revoke the session the token belongs to. Idempotent: no token, a bad one or an already
    revoked session all end the same way, because failing a logout protects nothing."""
    if not refresh_token:
        return
    try:
        claims = decode_refresh_token(refresh_token, secret)
    except InvalidTokenError:
        return
    await session.execute(
        update(AuthSession)
        .where(AuthSession.id == claims.session_id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=_now())
    )
    await session.commit()
```

- [ ] **Step 4: Убедиться, что тест проходит**

Run: `.venv/Scripts/python.exe -m pytest tests/auth/test_service.py -v`
Expected: PASS, 17 тестов.

- [ ] **Step 5: Линтеры и коммит**

Run: `.venv/Scripts/ruff.exe check . && .venv/Scripts/ruff.exe format --check . && .venv/Scripts/mypy.exe app`
Expected: чисто. Если mypy сообщает `"Result[Any]" has no attribute "rowcount"` на двух строках с `rowcount`, добавить к каждой `# type: ignore[attr-defined]` с комментарием строкой выше, как в `app/ingestion/normalize.py:308`: `# CursorResult carries rowcount; the base Result type mypy infers here does not.` Если не сообщает — не добавлять (`warn_unused_ignores = true`).

```bash
git add app/auth/service.py tests/auth/test_service.py
git commit -m "add login, refresh rotation with reuse detection, and logout"
```

---

### Task 7: HTTP — эндпоинты `/api/auth` и проверка прав

**Files:**
- Create: `app/auth/dependencies.py`, `app/schemas/auth.py`, `app/api/routes/auth.py`
- Modify: `app/api/router.py`, `Dockerfile.prod`
- Test: `tests/auth/test_dependencies.py`, `tests/auth/test_routes.py`

**Interfaces:**
- Consumes: `service.*` (Task 6), `tokens.*` (Task 2), фикстуры `api`, `make_user`, `fake_redis` (Task 5).
- Produces:
  - `async current_claims(credentials: HTTPAuthorizationCredentials | None) -> AccessClaims` — 401
  - `require_permission(code: str) -> Callable[..., Awaitable[AccessClaims]]` — 401 / 403
  - `REFRESH_COOKIE = "refresh_token"`, `COOKIE_PATH = "/api/auth"`, `INVALID_CREDENTIALS`, `SESSION_REJECTED` в `app/api/routes/auth.py`
  - `POST /api/auth/login`, `POST /api/auth/refresh`, `POST /api/auth/logout`, `GET /api/auth/me`

- [ ] **Step 1: Написать падающий тест зависимостей**

Create `tests/auth/test_dependencies.py`:

```python
"""Permission checks from the access token, without the database."""

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.auth.dependencies import require_permission
from app.auth.tokens import ACCESS_TTL, AccessClaims, issue_access_token, issue_refresh_token
from app.core.config import get_settings

CODE = "pipeline.run.status"


def _client() -> TestClient:
    app = FastAPI()

    @app.get("/guarded")
    async def guarded(claims: AccessClaims = Depends(require_permission(CODE))) -> dict[str, str]:
        return {"user": claims.username}

    return TestClient(app)


def _bearer(permissions: list[str], now: datetime | None = None) -> dict[str, str]:
    token = issue_access_token(
        user_id=uuid.uuid4(),
        username="adilet",
        display_name="Adilet",
        permissions=permissions,
        secret=get_settings().secret_key,
        now=now or datetime.now(UTC),
    )
    return {"Authorization": f"Bearer {token}"}


def test_no_token_is_401() -> None:
    response = _client().get("/guarded")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_a_garbage_token_is_401() -> None:
    assert _client().get("/guarded", headers={"Authorization": "Bearer x.y.z"}).status_code == 401


def test_another_scheme_is_401() -> None:
    assert _client().get("/guarded", headers={"Authorization": "Basic abc"}).status_code == 401


def test_an_expired_token_is_401() -> None:
    stale = datetime.now(UTC) - ACCESS_TTL - timedelta(seconds=5)
    assert _client().get("/guarded", headers=_bearer([CODE], stale)).status_code == 401


def test_a_refresh_token_is_not_a_bearer_token() -> None:
    token = issue_refresh_token(
        user_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        secret=get_settings().secret_key,
        now=datetime.now(UTC),
    )
    response = _client().get("/guarded", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


def test_a_missing_permission_is_403() -> None:
    response = _client().get("/guarded", headers=_bearer(["pipeline.runs.view"]))
    assert response.status_code == 403


def test_a_granted_permission_passes() -> None:
    response = _client().get("/guarded", headers=_bearer([CODE]))
    assert response.status_code == 200
    assert response.json() == {"user": "adilet"}
```

- [ ] **Step 2: Написать падающий тест маршрутов**

Create `tests/auth/test_routes.py`:

```python
"""The /api/auth endpoints over HTTP: status codes, the cookie, the headers."""

import httpx
import pytest

from app.api.routes import auth as auth_routes
from app.auth.lockout import LOCK_SECONDS, lock_key
from app.core.config import Settings
from tests.auth.conftest import PASSWORD, FakeRedis, MakeUser

LOGIN = "/api/auth/login"


def _set_cookie(response: httpx.Response) -> str:
    (header,) = [
        h for h in response.headers.get_list("set-cookie") if h.startswith("refresh_token=")
    ]
    return header


def _cookie_value(response: httpx.Response) -> str:
    return _set_cookie(response).split(";", 1)[0].removeprefix("refresh_token=")


async def _post_with_cookie(api: httpx.AsyncClient, path: str, token: str) -> httpx.Response:
    # Explicit, so a test decides exactly which token is presented; the client's jar stays out.
    api.cookies.clear()
    return await api.post(path, headers={"Cookie": f"refresh_token={token}"})


async def _login(api: httpx.AsyncClient) -> httpx.Response:
    response = await api.post(LOGIN, json={"username": "adilet", "password": PASSWORD})
    assert response.status_code == 200, response.text
    return response


async def test_login_answers_with_the_access_token_and_the_user(
    api: httpx.AsyncClient, make_user: MakeUser
) -> None:
    await make_user(permissions=["pipeline.runs.view"])
    body = (await _login(api)).json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 900
    assert body["access_token"]
    assert body["user"]["username"] == "adilet"
    assert body["user"]["display_name"] == "Adilet"
    assert body["user"]["permissions"] == ["pipeline.runs.view"]
    assert "refresh_token" not in body


async def test_the_refresh_cookie_is_locked_down(
    api: httpx.AsyncClient, make_user: MakeUser
) -> None:
    await make_user()
    header = _set_cookie(await _login(api))
    assert "HttpOnly" in header
    assert "samesite=strict" in header.lower()
    assert "Path=/api/auth" in header
    assert "Max-Age=604800" in header
    # Local: a browser would drop a Secure cookie set over plain http.
    assert "Secure" not in header


async def test_the_cookie_is_secure_in_production(
    api: httpx.AsyncClient, make_user: MakeUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    production = Settings(_env_file=None, app_env="production", secret_key="p" * 48)
    monkeypatch.setattr(auth_routes, "get_settings", lambda: production)
    await make_user()
    assert "Secure" in _set_cookie(await _login(api))


async def test_an_unknown_login_and_a_wrong_password_look_the_same(
    api: httpx.AsyncClient, make_user: MakeUser
) -> None:
    await make_user()
    wrong = await api.post(LOGIN, json={"username": "adilet", "password": "wrong"})
    unknown = await api.post(LOGIN, json={"username": "nobody", "password": PASSWORD})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json() == {"detail": auth_routes.INVALID_CREDENTIALS}


async def test_a_locked_login_is_429_with_retry_after(
    api: httpx.AsyncClient, make_user: MakeUser, fake_redis: FakeRedis
) -> None:
    await make_user()
    await fake_redis.set(lock_key("login", "adilet"), "1", ex=LOCK_SECONDS)
    response = await api.post(LOGIN, json={"username": "adilet", "password": PASSWORD})
    assert response.status_code == 429
    assert response.headers["retry-after"] == str(LOCK_SECONDS)


async def test_login_validates_the_body(api: httpx.AsyncClient) -> None:
    assert (await api.post(LOGIN, json={"username": "adilet"})).status_code == 422


async def test_refresh_returns_a_new_pair_and_a_new_cookie(
    api: httpx.AsyncClient, make_user: MakeUser
) -> None:
    await make_user()
    first = await _login(api)
    second = await _post_with_cookie(api, "/api/auth/refresh", _cookie_value(first))
    assert second.status_code == 200
    assert second.json()["access_token"]
    assert _cookie_value(second) != _cookie_value(first)


async def test_refresh_without_a_cookie_is_401(api: httpx.AsyncClient) -> None:
    api.cookies.clear()
    assert (await api.post("/api/auth/refresh")).status_code == 401


async def test_a_rejected_refresh_clears_the_cookie(
    api: httpx.AsyncClient, make_user: MakeUser
) -> None:
    await make_user()
    old = _cookie_value(await _login(api))
    await _post_with_cookie(api, "/api/auth/refresh", old)
    reused = await _post_with_cookie(api, "/api/auth/refresh", old)
    assert reused.status_code == 401
    assert reused.json() == {"detail": auth_routes.SESSION_REJECTED}
    assert "Max-Age=0" in _set_cookie(reused)


async def test_logout_clears_the_cookie_and_ends_the_session(
    api: httpx.AsyncClient, make_user: MakeUser
) -> None:
    await make_user()
    token = _cookie_value(await _login(api))
    response = await _post_with_cookie(api, "/api/auth/logout", token)
    assert response.status_code == 204
    assert "Max-Age=0" in _set_cookie(response)
    assert (await _post_with_cookie(api, "/api/auth/refresh", token)).status_code == 401


async def test_logout_without_a_cookie_is_still_204(api: httpx.AsyncClient) -> None:
    api.cookies.clear()
    assert (await api.post("/api/auth/logout")).status_code == 204


async def test_me_reads_the_bearer_token(api: httpx.AsyncClient, make_user: MakeUser) -> None:
    user = await make_user()
    access = (await _login(api)).json()["access_token"]
    response = await api.get("/api/auth/me", headers={"Authorization": f"Bearer {access}"})
    assert response.status_code == 200
    assert response.json() == {
        "id": str(user.id),
        "username": "adilet",
        "display_name": "Adilet",
        "permissions": list(user.permissions),
    }


async def test_me_without_a_token_is_401(api: httpx.AsyncClient) -> None:
    assert (await api.get("/api/auth/me")).status_code == 401
```

- [ ] **Step 3: Убедиться, что тесты падают**

Run: `.venv/Scripts/python.exe -m pytest tests/auth/test_dependencies.py tests/auth/test_routes.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.auth.dependencies'`.

- [ ] **Step 4: Реализовать зависимости**

Create `app/auth/dependencies.py`:

```python
"""FastAPI dependencies for protected endpoints.

Permissions are read from the access token and the database is not asked. Accepted consequence
(design, section 1): a revoked permission or session keeps working until the access token
already issued expires, at most `ACCESS_TTL`. Refresh after revocation does not succeed.

Protected endpoints take the token from the Authorization header only, never from a cookie,
which is what makes cross-site request forgery against them impossible.
"""

from collections.abc import Awaitable, Callable

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.auth.tokens import AccessClaims, InvalidTokenError, decode_access_token
from app.core.config import get_settings

_bearer = HTTPBearer(auto_error=False)


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="not authenticated",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def current_claims(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> AccessClaims:
    if credentials is None:
        raise _unauthorized()
    try:
        return decode_access_token(credentials.credentials, get_settings().secret_key)
    except InvalidTokenError:
        raise _unauthorized() from None


def require_permission(code: str) -> Callable[..., Awaitable[AccessClaims]]:
    async def dependency(claims: AccessClaims = Depends(current_claims)) -> AccessClaims:
        if code not in claims.permissions:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")
        return claims

    return dependency
```

- [ ] **Step 5: Реализовать схемы и маршруты**

Create `app/schemas/auth.py`:

```python
"""Authentication schemas (design 2026-09-11-auth-and-pipeline-panel, section 1)."""

import uuid
from typing import Literal

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    #: Bounded because Argon2 runs over whatever arrives: a megabyte password is CPU for free.
    password: str = Field(min_length=1, max_length=1024)


class AuthUser(BaseModel):
    id: uuid.UUID
    username: str
    display_name: str
    permissions: list[str]


class TokenResponse(BaseModel):
    """The access token only. The refresh token is in the cookie and never in a body a script
    can read."""

    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    user: AuthUser
```

Create `app/api/routes/auth.py`:

```python
"""Authentication endpoints (design 2026-09-11-auth-and-pipeline-panel, section 1).

The access token goes out in the response body and lives in the SPA's memory. The refresh token
goes out only as an HttpOnly cookie scoped to this router's path: no script can read it and no
other endpoint ever receives it.
"""

from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import service
from app.auth.dependencies import current_claims
from app.auth.tokens import ACCESS_TTL, REFRESH_TTL, AccessClaims
from app.core.config import get_settings
from app.core.redis import get_redis
from app.db.session import get_session
from app.schemas.auth import AuthUser, LoginRequest, TokenResponse

router = APIRouter(prefix="/auth", tags=["auth"])

REFRESH_COOKIE = "refresh_token"
#: Only /api/auth/* receives the cookie - refresh and logout are the only readers.
COOKIE_PATH = "/api/auth"
#: One text for an unknown login and a wrong password: two texts would list the logins.
INVALID_CREDENTIALS = "invalid username or password"
SESSION_REJECTED = "session expired"


def _secure() -> bool:
    # Browsers drop a Secure cookie set over plain http, and localhost is plain http.
    return get_settings().app_env == "production"


def _set_refresh_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        REFRESH_COOKIE,
        token,
        max_age=int(REFRESH_TTL.total_seconds()),
        path=COOKIE_PATH,
        secure=_secure(),
        httponly=True,
        # Strict, not Lax: the SPA and the API share one origin, so nothing legitimate is
        # cross-site and Strict costs nothing.
        samesite="strict",
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(
        REFRESH_COOKIE, path=COOKIE_PATH, secure=_secure(), httponly=True, samesite="strict"
    )


def _client_ip(request: Request) -> str:
    # Already the X-Forwarded-For address: uvicorn rewrites request.client behind Caddy.
    return request.client.host if request.client else "unknown"


def _issued(issued: service.IssuedTokens) -> JSONResponse:
    body = TokenResponse(
        access_token=issued.access_token,
        expires_in=int(ACCESS_TTL.total_seconds()),
        user=AuthUser(
            id=issued.user.id,
            username=issued.user.username,
            display_name=issued.user.display_name,
            permissions=list(issued.user.permissions),
        ),
    )
    response = JSONResponse(body.model_dump(mode="json"))
    _set_refresh_cookie(response, issued.refresh_token)
    return response


def _rejected() -> JSONResponse:
    response = JSONResponse(
        {"detail": SESSION_REJECTED}, status_code=status.HTTP_401_UNAUTHORIZED
    )
    _clear_refresh_cookie(response)
    return response


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> Response:
    try:
        issued = await service.login(
            session,
            redis,
            username=body.username,
            password=body.password,
            ip=_client_ip(request),
            secret=get_settings().secret_key,
        )
    except service.LoginLockedError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="too many failed attempts",
            headers={"Retry-After": str(exc.retry_after)},
        ) from None
    except service.InvalidCredentialsError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=INVALID_CREDENTIALS
        ) from None
    return _issued(issued)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    request: Request,
    refresh_token: Annotated[str | None, Cookie()] = None,
    session: AsyncSession = Depends(get_session),
) -> Response:
    if not refresh_token:
        return _rejected()
    try:
        issued = await service.refresh(
            session,
            refresh_token=refresh_token,
            ip=_client_ip(request),
            secret=get_settings().secret_key,
        )
    except service.RefreshRejectedError:
        return _rejected()
    return _issued(issued)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def logout(
    refresh_token: Annotated[str | None, Cookie()] = None,
    session: AsyncSession = Depends(get_session),
) -> Response:
    await service.logout(session, refresh_token=refresh_token, secret=get_settings().secret_key)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    _clear_refresh_cookie(response)
    return response


@router.get("/me", response_model=AuthUser)
async def me(claims: AccessClaims = Depends(current_claims)) -> AuthUser:
    return AuthUser(
        id=claims.user_id,
        username=claims.username,
        display_name=claims.display_name,
        permissions=list(claims.permissions),
    )
```

В `app/api/router.py` импорт маршрутов:

```python
from app.api.routes import auth, health, matches, model, teams, tournaments, ws
```

и после `api_router.include_router(health.router)`:

```python
api_router.include_router(auth.router)
```

- [ ] **Step 6: Комментарий у `--forwarded-allow-ips`**

В `Dockerfile.prod` заменить комментарий над `CMD [...]`:

```dockerfile
# Behind Caddy, so uvicorn binds every interface inside the container network and trusts the
# forwarded headers it is given - without those the client IP in the logs is Caddy's.
#
# `--forwarded-allow-ips "*"` trusts X-Forwarded-For from *anyone who reaches this port*. That is
# safe only because the port is not published: Caddy is the one thing that reaches it. The login
# lockout counts failures per address (app/auth/lockout.py), so publishing 8000 would let anyone
# send a fresh address with every guess and turn the per-address lock into decoration - and
# nothing here would say so.
```

- [ ] **Step 7: Убедиться, что тесты проходят**

Run: `.venv/Scripts/python.exe -m pytest tests/auth/test_dependencies.py tests/auth/test_routes.py tests/test_health.py -v`
Expected: PASS — 7 тестов зависимостей, 13 тестов маршрутов, тесты health.

- [ ] **Step 8: Линтеры и коммит**

Run: `.venv/Scripts/ruff.exe check . && .venv/Scripts/ruff.exe format --check . && .venv/Scripts/mypy.exe app`
Expected: чисто.

```bash
git add app/auth/dependencies.py app/schemas/auth.py app/api/routes/auth.py app/api/router.py Dockerfile.prod tests/auth/test_dependencies.py tests/auth/test_routes.py
git commit -m "add auth endpoints and permission checks"
```

---

### Task 8: CLI управления пользователем

**Files:**
- Create: `app/auth/cli.py`
- Test: `tests/auth/test_cli.py`

**Interfaces:**
- Consumes: `User`, `AuthSession` (Task 4), `hash_password`, `verify_password` (Task 2), `known_permissions` (Task 3), `revoke_all_sessions` (Task 6).
- Produces:
  - `MIN_PASSWORD_LENGTH = 12`
  - `class UsageError(Exception)`
  - `read_new_password(prompt: Callable[[str], str]) -> str`
  - `async create_user(session_factory, *, username: str, display_name: str, password: str) -> User`
  - `async set_password(session_factory, *, username: str, password: str) -> int` — число отозванных сессий
  - `async revoke_sessions(session_factory, *, username: str) -> int`
  - `async grant_all(session_factory, *, username: str) -> list[str]` — добавленные коды
  - `build_parser() -> argparse.ArgumentParser`, `main() -> None`

Функции принимают фабрику сессий параметром и никогда не зовут `get_session_factory()` сами: так уже однажды тест прогнался по боевой базе (`run_normalize`).

- [ ] **Step 1: Написать падающий тест**

Create `tests/auth/test_cli.py`:

```python
"""User management from the command line (design, section 1)."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth import cli
from app.auth.passwords import verify_password
from app.auth.permissions import RUNS_VIEW, known_permissions
from app.db.models.auth import AuthSession, User
from tests.auth.conftest import MakeUser

Sessions = async_sessionmaker[AsyncSession]
NEW_PASSWORD = "a brand new passphrase"


def _prompt(*answers: str) -> cli.PasswordPrompt:
    replies: Iterator[str] = iter(answers)
    return lambda _label: next(replies)


async def _user(sessions: Sessions, username: str = "adilet") -> User:
    async with sessions() as session:
        user = await session.scalar(select(User).where(User.username == username))
    assert user is not None
    return user


async def _add_session(sessions: Sessions, user: User) -> None:
    async with sessions() as session:
        session.add(
            AuthSession(
                user_id=user.id, token_hash="h", expires_at=datetime.now(UTC) + timedelta(days=1)
            )
        )
        await session.commit()


async def _live_sessions(sessions: Sessions) -> int:
    async with sessions() as session:
        rows = await session.scalars(select(AuthSession).where(AuthSession.revoked_at.is_(None)))
        return len(list(rows))


class TestReadNewPassword:
    def test_returns_a_password_typed_twice(self) -> None:
        assert cli.read_new_password(_prompt(NEW_PASSWORD, NEW_PASSWORD)) == NEW_PASSWORD

    def test_refuses_a_mismatch(self) -> None:
        with pytest.raises(cli.UsageError, match="do not match"):
            cli.read_new_password(_prompt(NEW_PASSWORD, NEW_PASSWORD + "x"))

    def test_refuses_a_short_password(self) -> None:
        short = "x" * (cli.MIN_PASSWORD_LENGTH - 1)
        with pytest.raises(cli.UsageError, match="at least"):
            cli.read_new_password(_prompt(short, short))


class TestCreateUser:
    async def test_grants_every_known_permission(self, sessionmaker: Sessions) -> None:
        await cli.create_user(
            sessionmaker, username="adilet", display_name="Adilet", password=NEW_PASSWORD
        )
        user = await _user(sessionmaker)
        assert user.display_name == "Adilet"
        assert user.permissions == list(known_permissions())
        assert verify_password(user.password_hash, NEW_PASSWORD)

    async def test_refuses_a_taken_username(self, sessionmaker: Sessions) -> None:
        await cli.create_user(
            sessionmaker, username="adilet", display_name="Adilet", password=NEW_PASSWORD
        )
        with pytest.raises(cli.UsageError, match="already exists"):
            await cli.create_user(
                sessionmaker, username="adilet", display_name="Other", password=NEW_PASSWORD
            )


class TestSetPassword:
    async def test_changes_the_password_and_signs_out_everywhere(
        self, sessionmaker: Sessions, make_user: MakeUser
    ) -> None:
        user = await make_user()
        await _add_session(sessionmaker, user)
        await _add_session(sessionmaker, user)

        revoked = await cli.set_password(sessionmaker, username="adilet", password=NEW_PASSWORD)

        assert revoked == 2
        assert await _live_sessions(sessionmaker) == 0
        assert verify_password((await _user(sessionmaker)).password_hash, NEW_PASSWORD)

    async def test_refuses_an_unknown_user(self, sessionmaker: Sessions) -> None:
        with pytest.raises(cli.UsageError, match="no user"):
            await cli.set_password(sessionmaker, username="nobody", password=NEW_PASSWORD)


class TestRevokeSessions:
    async def test_revokes_only_that_users_sessions(
        self, sessionmaker: Sessions, make_user: MakeUser
    ) -> None:
        adilet = await make_user()
        other = await make_user(username="other")
        await _add_session(sessionmaker, adilet)
        await _add_session(sessionmaker, other)

        assert await cli.revoke_sessions(sessionmaker, username="adilet") == 1
        assert await _live_sessions(sessionmaker) == 1


class TestGrantAll:
    async def test_adds_only_the_missing_codes(
        self, sessionmaker: Sessions, make_user: MakeUser
    ) -> None:
        await make_user(permissions=[RUNS_VIEW])
        added = await cli.grant_all(sessionmaker, username="adilet")
        assert added == [code for code in known_permissions() if code != RUNS_VIEW]
        assert (await _user(sessionmaker)).permissions == [RUNS_VIEW, *added]

    async def test_says_so_when_nothing_is_missing(
        self, sessionmaker: Sessions, make_user: MakeUser
    ) -> None:
        await make_user(permissions=list(known_permissions()))
        assert await cli.grant_all(sessionmaker, username="adilet") == []


def test_the_password_is_never_an_argument() -> None:
    """A password in argv stays in shell history and in the process list."""
    parser = cli.build_parser()
    for command in ("create-user", "set-password"):
        args = ["--username", "adilet"]
        if command == "create-user":
            args += ["--display-name", "Adilet"]
        with pytest.raises(SystemExit):
            parser.parse_args([command, *args, "--password", "x"])
```

- [ ] **Step 2: Убедиться, что тест падает**

Run: `.venv/Scripts/python.exe -m pytest tests/auth/test_cli.py -v`
Expected: FAIL — `ImportError: cannot import name 'cli' from 'app.auth'`.

- [ ] **Step 3: Реализовать**

Create `app/auth/cli.py`:

```python
"""Manage the users of the admin panel.

    docker compose -f docker-compose.prod.yml run --rm api \\
      python -m app.auth.cli create-user --username adilet --display-name "Adilet"

Run it without `-T` and without `</dev/null`: the password is read with getpass, which needs a
terminal. It is never an argument - that would leave it in shell history and in the process list.
"""

import argparse
import asyncio
import getpass
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.passwords import hash_password
from app.auth.permissions import known_permissions
from app.auth.service import revoke_all_sessions
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.models.auth import User
from app.db.session import dispose_engine, get_session_factory

MIN_PASSWORD_LENGTH = 12

PasswordPrompt = Callable[[str], str]
Sessions = async_sessionmaker[AsyncSession]


class UsageError(Exception):
    """A mistake the operator can fix, printed without a traceback."""


def read_new_password(prompt: PasswordPrompt) -> str:
    first = prompt("New password: ")
    second = prompt("Repeat the password: ")
    if first != second:
        raise UsageError("passwords do not match")
    if len(first) < MIN_PASSWORD_LENGTH:
        raise UsageError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    return first


async def _find(session: AsyncSession, username: str) -> User:
    user = await session.scalar(select(User).where(User.username == username))
    if user is None:
        raise UsageError(f"no user {username!r}")
    return user


async def create_user(
    session_factory: Sessions, *, username: str, display_name: str, password: str
) -> User:
    async with session_factory() as session:
        user = User(
            username=username,
            display_name=display_name,
            password_hash=await asyncio.to_thread(hash_password, password),
            permissions=list(known_permissions()),
        )
        session.add(user)
        try:
            await session.commit()
        except IntegrityError as exc:
            raise UsageError(f"user {username!r} already exists") from exc
        return user


async def set_password(session_factory: Sessions, *, username: str, password: str) -> int:
    """Change the password and revoke every session: whoever knew the old one is out."""
    async with session_factory() as session:
        user = await _find(session, username)
        user.password_hash = await asyncio.to_thread(hash_password, password)
        revoked = await revoke_all_sessions(session, user.id)
        await session.commit()
        return revoked


async def revoke_sessions(session_factory: Sessions, *, username: str) -> int:
    async with session_factory() as session:
        user = await _find(session, username)
        revoked = await revoke_all_sessions(session, user.id)
        await session.commit()
        return revoked


async def grant_all(session_factory: Sessions, *, username: str) -> list[str]:
    """Add the codes that appeared since the user was created. A new command's button stays
    hidden until this runs - `create-user` hands out only what existed at the time."""
    async with session_factory() as session:
        user = await _find(session, username)
        missing = [code for code in known_permissions() if code not in user.permissions]
        # A new list, not an in-place append: an ARRAY column mutated in place is not seen as
        # changed and the update would silently not happen.
        user.permissions = [*user.permissions, *missing]
        await session.commit()
        return missing


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="app.auth.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create-user", help="create a user with every known permission")
    create.add_argument("--username", required=True)
    create.add_argument("--display-name", required=True)

    password = sub.add_parser("set-password", help="change the password, revoke all sessions")
    password.add_argument("--username", required=True)

    revoke = sub.add_parser("revoke-sessions", help="sign the user out everywhere")
    revoke.add_argument("--username", required=True)

    grant = sub.add_parser("grant-all", help="grant every permission known today")
    grant.add_argument("--username", required=True)
    return parser


async def run(args: argparse.Namespace, session_factory: Sessions, prompt: PasswordPrompt) -> None:
    if args.command == "create-user":
        user = await create_user(
            session_factory,
            username=args.username,
            display_name=args.display_name,
            password=read_new_password(prompt),
        )
        print(f"created {user.username} with {len(user.permissions)} permissions")
    elif args.command == "set-password":
        revoked = await set_password(
            session_factory, username=args.username, password=read_new_password(prompt)
        )
        print(f"password changed; {revoked} sessions revoked")
    elif args.command == "revoke-sessions":
        revoked = await revoke_sessions(session_factory, username=args.username)
        print(f"{revoked} sessions revoked")
    else:
        added = await grant_all(session_factory, username=args.username)
        if added:
            print("granted:")
            for code in added:
                print(f"  {code}")
        else:
            print("nothing to grant: the user already has every known permission")


def main() -> None:
    args = build_parser().parse_args()
    configure_logging(get_settings().log_level)

    async def go() -> None:
        try:
            await run(args, get_session_factory(), getpass.getpass)
        finally:
            await dispose_engine()

    try:
        asyncio.run(go())
    except UsageError as exc:
        raise SystemExit(f"error: {exc}") from None


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Убедиться, что тест проходит**

Run: `.venv/Scripts/python.exe -m pytest tests/auth/test_cli.py -v`
Expected: PASS, 11 тестов.

Run: `.venv/Scripts/python.exe -m app.auth.cli --help`
Expected: список из четырёх подкоманд.

- [ ] **Step 5: Линтеры и коммит**

Run: `.venv/Scripts/ruff.exe check . && .venv/Scripts/ruff.exe format --check . && .venv/Scripts/mypy.exe app`
Expected: чисто.

```bash
git add app/auth/cli.py tests/auth/test_cli.py
git commit -m "add a command line for creating and managing users"
```

---

### Task 9: Проверка целиком

Кода в этой задаче нет — только прогоны. Если что-то не проходит, чинить в той задаче, к которой относится поломка, и коммитить отдельным коммитом.

- [ ] **Step 1: Прогон как в CI**

Run: `docker compose build tools`
Run: `docker compose run --rm -e STRATZ_API_TOKEN= -e STEAM_API_KEY= tools python -m pytest -p no:warnings -q`
Expected: **976 passed, 2 skipped** (890 до начала + 86 новых). Число пропущенных — те же 2, что и до начала: новые тесты с БД пропущенными быть не должны.

Run: `docker compose run --rm tools sh -c "ruff check . && ruff format --check . && mypy app"`
Expected: чисто.

- [ ] **Step 2: Живой прогон против запущенного API**

Run: `docker compose up -d postgres redis`
Run: `docker compose run --rm tools alembic upgrade head`

Создать пользователя без терминала — через функцию, а не через `getpass`:

```bash
docker compose run --rm tools python -c "import asyncio; from app.auth.cli import create_user; from app.db.session import get_session_factory; asyncio.run(create_user(get_session_factory(), username='smoke', display_name='Smoke', password='smoke-test-password'))"
```

В отдельном терминале: `.venv/Scripts/python.exe -m uvicorn app.main:app --port 8100`

```bash
curl -s -c jar.txt -H "Content-Type: application/json" -d '{"username":"smoke","password":"smoke-test-password"}' http://localhost:8100/api/auth/login
# expected: {"access_token": "...", "token_type": "bearer", "expires_in": 900, "user": {...}}
TOKEN=<access_token из ответа>
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8100/api/auth/me
# expected: {"id": "...", "username": "smoke", ...}
curl -s -b jar.txt -c jar.txt -X POST http://localhost:8100/api/auth/refresh -o /dev/null -w "%{http_code}\n"
# expected: 200
curl -s -b jar.txt -X POST http://localhost:8100/api/auth/logout -o /dev/null -w "%{http_code}\n"
# expected: 204
for i in 1 2 3 4 5 6; do curl -s -o /dev/null -w "%{http_code} " -H "Content-Type: application/json" -d '{"username":"smoke","password":"wrong"}' http://localhost:8100/api/auth/login; done; echo
# expected: 401 401 401 401 401 429
```

Убрать за собой: остановить uvicorn, `rm jar.txt`, удалить пользователя и ключи блокировки:

```bash
docker compose exec postgres psql -U dota -d dota_oracle -c "delete from users where username = 'smoke'"
docker compose exec redis redis-cli --scan --pattern "auth:*" | xargs -r docker compose exec -T redis redis-cli del
```

- [ ] **Step 3: Проверить, что прод без ключа отказывается стартовать**

Run: `docker compose run --rm -e APP_ENV=production -e SECRET_KEY= tools python -c "from app.core.config import get_settings; get_settings()"`
Expected: `ValidationError ... SECRET_KEY must be at least 32 bytes in production`.

---

### Task 10: Документация

**Files:**
- Modify: `CLAUDE.md` (бэкенд-репозиторий)
- Modify: `docs/superpowers/specs/2026-09-11-auth-and-pipeline-panel-design.md`
- Modify: `../CLAUDE.md` (корень рабочей директории, вне git — не коммитится)

- [ ] **Step 1: `CLAUDE.md` бэкенда**

В раздел «Текущее состояние», последним пунктом списка:

```markdown
- **Авторизация (раздел 1 дизайна `2026-09-11-auth-and-pipeline-panel`) — 14.09.2026.**
  Пакет `app/auth/`: Argon2id, JWT HS256 (access 15 минут в теле, refresh 7 дней в
  `HttpOnly`-cookie с `Path=/api/auth` и `SameSite=Strict`), таблица сессий на устройство,
  ротация refresh с детектом повторного предъявления — отзываются **все** сессии пользователя.
  Блокировка: 5 неудач на логин и 20 на адрес за 15 минут. Права — по access-токену без базы,
  `require_permission(code)`; отозванное право живёт до конца уже выданного access-токена.
  **Ротация — условный `UPDATE` по предъявленному хешу**, иначе два одновременных `refresh`
  одним токеном оба проходят проверку и детектор слеп. Коды прав — `app/auth/permissions.py`,
  тест держит их равными подкомандам `app.ingestion.cli`; раздел 2 заменит источник реестром.
  **Пользователя создаёт только CLI**, пароль только через `getpass`:
  `python -m app.auth.cli create-user|set-password|revoke-sessions|grant-all`.
  **`SECRET_KEY` в production обязателен** (≥ 32 байт, не dev-значение) — без него не стартуют
  API, воркер и контейнер миграций; `deploy.sh` проверяет это до `pull`.
  Раздел 2 (запуск команд) и фронтенд — ещё нет.
```

В раздел «Команды», после блока «фаза 2: разметка тиров и форматов», внутри того же блока кода:

```bash
# пользователи админ-панели (пароль спрашивается через getpass; на проде без -T)
docker compose run --rm tools python -m app.auth.cli create-user --username adilet --display-name "Адилет"
docker compose run --rm tools python -m app.auth.cli grant-all --username adilet
```

- [ ] **Step 2: Спека**

В `docs/superpowers/specs/2026-09-11-auth-and-pipeline-panel-design.md` строку статуса заменить на:

```markdown
Дата: 2026-09-11. Статус: раздел 1 реализован 14.09.2026 (`feature/auth`,
план `docs/superpowers/plans/2026-09-14-auth-backend.md`); разделы 2 и 3 — нет.
```

В конец раздела 1, после подраздела `SECRET_KEY`, добавить:

```markdown
### Решения, принятые при реализации

- Коды `pipeline.run.<команда>` до появления реестра (раздел 2) перечислены в
  `app/auth/permissions.py`; тест сверяет их с подкомандами `app.ingestion.cli` кроме
  `liquipedia`. В список вошла `backfill-segments`, появившаяся после этой спеки.
- `/logout` без cookie или с негодной — `204` и очистка cookie.
- Минимальная длина пароля — 12 символов, проверяет CLI.
- Ротация refresh — условный `UPDATE ... WHERE token_hash = <предъявленный>`; не обновилась ни
  одна строка — это повторное предъявление со всеми последствиями.
- В production отвергается не только пустой и короткий `SECRET_KEY`, но и dev-значение по
  умолчанию; `deploy.sh` проверяет ключ до выката.

### Открытый вопрос

Две вкладки, открытые одновременно (восстановление сессии браузера), вызовут `POST /refresh`
с одной cookie. Вторая предъявит уже использованный токен, и детектор отзовёт сессии на всех
устройствах. Single-flight из раздела 3 работает только внутри вкладки. Решение — окно в
несколько секунд, в котором принимается предыдущий хеш, — это отступление от спеки, и
принимать его владельцу, до реализации раздела 3.
```

- [ ] **Step 3: Корневой `CLAUDE.md`**

В `../CLAUDE.md`, в начало раздела «Текущий шаг», перед подразделом «Сегменты»:

```markdown
### Авторизация: бэкенд (14.09.2026)

Раздел 1 дизайна `2026-09-11-auth-and-pipeline-panel` реализован в `feature/auth` бэкенда:
вход, refresh с ротацией и детектом кражи, блокировка перебора, права по токену, CLI
пользователя. Разделы 2 (кнопки запуска команд) и 3 (фронтенд) — отдельные планы, не начаты.
Перед выкатом на прод нужен `SECRET_KEY` в `.env` сервера и обновлённый `deploy.sh` на
сервере. Открытый вопрос для владельца — одновременный refresh из двух вкладок (в спеке).
```

- [ ] **Step 4: Коммит**

```bash
git add CLAUDE.md docs/superpowers/specs/2026-09-11-auth-and-pipeline-panel-design.md docs/superpowers/plans/2026-09-14-auth-backend.md
git commit -m "document backend authentication"
```

Ветку не пушить и в `development` не мерджить без просьбы владельца.
