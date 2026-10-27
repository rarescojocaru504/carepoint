import pytest
from fastapi.testclient import TestClient

from app.core.db import get_db
from app.core.redis import get_redis
from app.main import app


class StubSession:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    async def execute(self, _statement: object) -> None:
        if self.fail:
            raise ConnectionRefusedError("db down")


class StubRedis:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    async def ping(self) -> bool:
        if self.fail:
            raise ConnectionRefusedError("redis down")
        return True


def _override(db_fails: bool, redis_fails: bool) -> None:
    async def db() -> StubSession:
        return StubSession(fail=db_fails)

    async def redis() -> StubRedis:
        return StubRedis(fail=redis_fails)

    app.dependency_overrides[get_db] = db
    app.dependency_overrides[get_redis] = redis


def test_ready_ok_when_all_dependencies_up(client: TestClient) -> None:
    _override(db_fails=False, redis_fails=False)

    response = client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "checks": {"database": "ok", "redis": "ok"},
    }


@pytest.mark.parametrize(
    ("db_fails", "redis_fails", "expected_checks"),
    [
        (True, False, {"database": "unavailable", "redis": "ok"}),
        (False, True, {"database": "ok", "redis": "unavailable"}),
        (True, True, {"database": "unavailable", "redis": "unavailable"}),
    ],
)
def test_ready_503_when_a_dependency_is_down(
    client: TestClient,
    db_fails: bool,
    redis_fails: bool,
    expected_checks: dict[str, str],
) -> None:
    _override(db_fails=db_fails, redis_fails=redis_fails)

    response = client.get("/ready")

    assert response.status_code == 503
    body = response.json()
    assert body == {"status": "unavailable", "checks": expected_checks}
    assert "down" not in response.text  # error details are logged, not exposed


@pytest.mark.integration
@pytest.mark.usefixtures("require_services")
def test_ready_against_real_services(client: TestClient) -> None:
    response = client.get("/ready")

    assert response.status_code == 200
    assert response.json()["checks"] == {"database": "ok", "redis": "ok"}
