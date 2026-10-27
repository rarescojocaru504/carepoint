import os
import socket
from collections.abc import Iterator
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import app

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
def test_database_url(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Point application settings at the disposable test database."""
    if not _reachable(TEST_DATABASE_URL, 5432):
        _skip_or_fail("Test PostgreSQL database is not reachable")
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    get_settings.cache_clear()
    yield TEST_DATABASE_URL
    get_settings.cache_clear()
