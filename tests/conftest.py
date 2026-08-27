from collections.abc import AsyncIterator
from contextlib import suppress

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.db import models  # noqa: F401  - populates Base.metadata
from app.db.base import Base
from app.main import create_app

#: Database tests build the whole schema in a throwaway one. Isolation is done with
#: `search_path` rather than SQLAlchemy's schema_translate_map, because the latter only
#: rewrites ORM constructs: hand-written `text()` statements ignore it and would silently
#: hit the real tables instead. search_path covers both.
TEST_SCHEMA = "pytest_tmp"


@pytest.fixture
def client() -> TestClient:
    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture
async def sessionmaker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Session factory bound to a disposable schema.

    Skips rather than fails when there is no database: the rest of the suite is DB-free and
    must stay runnable on a laptop with nothing running.
    """
    url = get_settings().database_url
    try:
        # create_async_engine already raises when the asyncpg driver is missing, so it has
        # to sit inside the guard too.
        admin = create_async_engine(url, poolclass=None)
        async with admin.connect() as connection:
            await connection.execute(text("select 1"))
    except Exception as exc:
        with suppress(Exception, NameError):
            await admin.dispose()
        pytest.skip(f"database not reachable: {type(exc).__name__}")

    async with admin.begin() as connection:
        await connection.execute(text(f"drop schema if exists {TEST_SCHEMA} cascade"))
        await connection.execute(text(f"create schema {TEST_SCHEMA}"))

    # Every connection from this engine resolves unqualified names into the test schema,
    # so ORM statements and raw text() alike stay off the real tables.
    scoped = create_async_engine(
        url,
        poolclass=None,
        connect_args={"server_settings": {"search_path": TEST_SCHEMA}},
    )
    async with scoped.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    try:
        yield async_sessionmaker(scoped, expire_on_commit=False)
    finally:
        await scoped.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(f"drop schema if exists {TEST_SCHEMA} cascade"))
        await admin.dispose()


@pytest.fixture
async def session(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with sessionmaker() as db_session:
        yield db_session
