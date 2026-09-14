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

    async def decr(self, name: str) -> int:
        # Like Redis: a missing key starts at 0, and the result keeps whatever expiry it had.
        value = int(self.values.get(name, "0")) - 1
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
