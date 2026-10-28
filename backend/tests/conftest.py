import os
import socket

# Tests drive the sweeper and reconciliation directly; keep the app's own
# background loops off (set before the app and its settings are imported).
os.environ.setdefault("BACKGROUND_WORKERS_ENABLED", "false")
from collections.abc import AsyncIterator, Iterator
from urllib.parse import urlsplit

import httpx
import pytest
from alembic import command
from fastapi.testclient import TestClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.core.db import get_db
from app.core.redis import get_redis
from app.main import app
from tests.helpers import alembic_config
from tests.world import World

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://carepoint:carepoint@localhost:5432/carepoint_test",
)

# CI sets REQUIRE_SERVICES=1 so a broken service fails the run instead of skipping tests.
REQUIRE_SERVICES = os.environ.get("REQUIRE_SERVICES") == "1"


def _skip_or_fail(reason: str) -> None:
    if REQUIRE_SERVICES:
        pytest.fail(f"{reason} (REQUIRE_SERVICES=1)")
    pytest.skip(reason)


def _reachable(url: str, default_port: int) -> bool:
    parts = urlsplit(url)
    try:
        with socket.create_connection(
            (parts.hostname or "localhost", parts.port or default_port), timeout=1
        ):
            return True
    except OSError:
        return False


@pytest.fixture
def client() -> Iterator[TestClient]:
    """TestClient with a clean slate of dependency overrides."""
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def require_services() -> None:
    """Skip (or fail, under REQUIRE_SERVICES=1) when PostgreSQL or Redis is down."""
    settings = get_settings()
    if not _reachable(settings.database_url, 5432):
        _skip_or_fail("PostgreSQL is not reachable")
    if not _reachable(settings.redis_url, 6379):
        _skip_or_fail("Redis is not reachable")


@pytest.fixture
def test_database_url() -> str:
    """URL of the disposable test database (skips/fails if it is unreachable)."""
    if not _reachable(TEST_DATABASE_URL, 5432):
        _skip_or_fail("Test PostgreSQL database is not reachable")
    return TEST_DATABASE_URL


@pytest.fixture(scope="session")
def migrated_database_url() -> str:
    """Test database upgraded to the latest migration, once per test session."""
    if not _reachable(TEST_DATABASE_URL, 5432):
        _skip_or_fail("Test PostgreSQL database is not reachable")
    command.upgrade(alembic_config(TEST_DATABASE_URL), "head")
    return TEST_DATABASE_URL


@pytest.fixture
async def db_session(migrated_database_url: str) -> AsyncIterator[AsyncSession]:
    """An AsyncSession whose work is rolled back after the test.

    The session joins an outer transaction via savepoints, so code under test may
    call `commit()` freely; nothing ever persists. This matters for the
    append-only `stock_movements` table, which cannot be cleaned up any other way.
    """
    engine = create_async_engine(migrated_database_url, poolclass=NullPool)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        session = AsyncSession(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            yield session
        finally:
            await session.close()
            await transaction.rollback()
    await engine.dispose()


def _test_redis_url() -> str:
    # Same server as the app, but logical DB 15, so flushing it is harmless.
    return urlsplit(get_settings().redis_url)._replace(path="/15").geturl()


@pytest.fixture
async def redis() -> AsyncIterator[Redis]:
    """A Redis client on an isolated logical DB, flushed before and after the test."""
    url = _test_redis_url()
    if not _reachable(url, 6379):
        _skip_or_fail("Redis is not reachable")
    client = Redis.from_url(url, decode_responses=True)
    await client.flushdb()
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


@pytest.fixture
async def api(db_session: AsyncSession, redis: Redis) -> AsyncIterator[httpx.AsyncClient]:
    """In-process HTTP client wired to the rolled-back `db_session` and test Redis.

    Runs on the test's own event loop (unlike TestClient), so it can share the
    asyncpg connection behind `db_session`.
    """

    async def _db() -> AsyncIterator[AsyncSession]:
        yield db_session

    async def _redis() -> Redis:
        return redis

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_redis] = _redis
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
async def world(migrated_database_url: str, redis: Redis) -> AsyncIterator[World]:
    """Committed rows + app wired to a session per request (see tests/world.py)."""
    engine = create_async_engine(migrated_database_url, poolclass=NullPool)
    w = World(async_sessionmaker(engine, expire_on_commit=False))

    async def _db() -> AsyncIterator[AsyncSession]:
        async with w.sessions() as session:
            yield session

    async def _redis() -> Redis:
        return redis

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_redis] = _redis
    try:
        yield w
    finally:
        app.dependency_overrides.clear()
        await w.cleanup()
        await engine.dispose()
