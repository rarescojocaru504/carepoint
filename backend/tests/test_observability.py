"""Request IDs, structured logs and GET /metrics (ADR-029)."""

import io
import json
import logging
import uuid

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import JsonFormatter, configure_logging, request_id_var
from app.core.security import create_access_token
from app.models import User, UserRole

# --- request ids --------------------------------------------------------------------


async def test_every_response_carries_a_request_id(api: httpx.AsyncClient) -> None:
    first = await api.get("/health")
    second = await api.get("/health")
    ids = [first.headers["X-Request-ID"], second.headers["X-Request-ID"]]
    assert all(len(i) == 32 for i in ids)
    assert ids[0] != ids[1]


async def test_a_well_formed_incoming_request_id_is_kept(api: httpx.AsyncClient) -> None:
    response = await api.get("/health", headers={"X-Request-ID": "lb-7f3a9c21.req_01"})
    assert response.headers["X-Request-ID"] == "lb-7f3a9c21.req_01"


@pytest.mark.parametrize(
    "bad", ["short", "x" * 65, "has space inside", "inject\\nnewline", "drop'; --", "a/b/c/d/e/f"]
)
async def test_a_malformed_request_id_is_replaced(api: httpx.AsyncClient, bad: str) -> None:
    response = await api.get("/health", headers={"X-Request-ID": bad})
    assert response.headers["X-Request-ID"] != bad
    assert len(response.headers["X-Request-ID"]) == 32


async def test_errors_carry_a_request_id_too(api: httpx.AsyncClient) -> None:
    response = await api.get("/api/v1/me")
    assert response.status_code == 401
    assert "X-Request-ID" in response.headers


# --- structured logs ------------------------------------------------------------------


async def test_each_request_logs_one_access_line_with_its_id(
    api: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="carepoint.access")
    machine_id = uuid.uuid4()
    response = await api.get(f"/api/v1/machines/{machine_id}")

    [line] = [r for r in caplog.records if r.name == "carepoint.access"]
    assert line.request_id == response.headers["X-Request-ID"]  # type: ignore[attr-defined]
    assert (line.method, line.status) == ("GET", 404)  # type: ignore[attr-defined]
    # The route template, never the id itself.
    assert line.route == "/api/v1/machines/{machine_id}"  # type: ignore[attr-defined]
    assert line.duration_ms >= 0  # type: ignore[attr-defined]


async def test_probes_log_quietly(api: httpx.AsyncClient, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="carepoint.access")
    await api.get("/health")
    assert [r for r in caplog.records if r.name == "carepoint.access"] == []


def test_json_lines_carry_the_request_id_extras_and_exceptions() -> None:
    stream = io.StringIO()
    configure_logging("json", "INFO", stream=stream)
    logger = logging.getLogger("carepoint.test")
    token = request_id_var.set("req-12345678")
    try:
        logger.info("paid %s", "r1", extra={"reservation_id": "r1"})
        try:
            raise ValueError("boom")
        except ValueError:
            logger.exception("failed")
    finally:
        request_id_var.reset(token)
        configure_logging("json", "INFO")  # back to stdout
    first, second = (json.loads(line) for line in stream.getvalue().splitlines())
    assert first["message"] == "paid r1"
    assert (first["level"], first["logger"]) == ("INFO", "carepoint.test")
    assert (first["request_id"], first["reservation_id"]) == ("req-12345678", "r1")
    assert first["ts"].endswith("+00:00")
    assert "ValueError: boom" in second["exc"]


def test_uvicorn_logs_go_through_the_same_handler() -> None:
    configure_logging("json", "INFO")
    configure_logging("json", "INFO")  # idempotent: still one handler
    ours = [h for h in logging.getLogger().handlers if h.get_name() == "carepoint"]
    assert len(ours) == 1
    assert logging.getLogger("uvicorn.error").propagate is True
    assert logging.getLogger("uvicorn.access").disabled is True


def test_text_format_for_local_reading() -> None:
    stream = io.StringIO()
    configure_logging("text", "INFO", stream=stream)
    try:
        logging.getLogger("carepoint.test").warning("hello")
    finally:
        configure_logging("json", "INFO")
    assert "WARNING" in stream.getvalue() and "carepoint.test" in stream.getvalue()
    assert "hello" in stream.getvalue()


def test_formatter_never_fails_on_odd_extras() -> None:
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "m", None, None)
    record.when = object()
    record.count = 3
    entry = json.loads(JsonFormatter().format(record))
    assert entry["message"] == "m"
    assert entry["count"] == 3
    assert "when" not in entry  # an object's repr is noise, not data


# --- metrics ------------------------------------------------------------------------


async def _user(db_session: AsyncSession, role: UserRole) -> dict[str, str]:
    user = User(email=f"{role.value}-{uuid.uuid4().hex[:8]}@example.com", password_hash="!",
                full_name="Someone", role=role)  # fmt: skip
    db_session.add(user)
    await db_session.flush()
    return {"Authorization": f"Bearer {create_access_token(user.id, user.role)[0]}"}


@pytest.mark.integration
async def test_metrics_are_not_public(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    assert (await api.get("/metrics")).status_code == 401
    customer = await _user(db_session, UserRole.CUSTOMER)
    assert (await api.get("/metrics", headers=customer)).status_code == 403
    wrong = {"Authorization": "Bearer not-the-metrics-token"}
    assert (await api.get("/metrics", headers=wrong)).status_code == 401


@pytest.mark.integration
async def test_operators_and_the_scraper_token_can_read_metrics(
    api: httpx.AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    operator = await _user(db_session, UserRole.OPERATOR)
    assert (await api.get("/metrics", headers=operator)).status_code == 200

    token = "m" * 40
    monkeypatch.setattr(get_settings(), "metrics_token", SecretStr(token))
    response = await api.get("/metrics", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")


@pytest.mark.integration
async def test_metrics_count_requests_by_route_template(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    operator = await _user(db_session, UserRole.OPERATOR)
    for _ in range(3):
        await api.get(f"/api/v1/machines/{uuid.uuid4()}")
    body = (await api.get("/metrics", headers=operator)).text

    template_line = next(
        line
        for line in body.splitlines()
        if line.startswith("carepoint_http_requests_total{")
        and 'route="/api/v1/machines/{machine_id}"' in line
        and 'status="404"' in line
    )
    assert float(template_line.rsplit(" ", 1)[1]) >= 3
    assert "carepoint_http_request_duration_seconds_bucket" in body
    # No raw ids in any label.
    assert "/api/v1/machines/0" not in body and "-4" not in body.split("route=")[-1][:40]


@pytest.mark.integration
async def test_metrics_include_business_gauges(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    operator = await _user(db_session, UserRole.OPERATOR)
    body = (await api.get("/metrics", headers=operator)).text
    for name in (
        'carepoint_machines{state="online"}',
        'carepoint_machines{state="maintenance"}',
        'carepoint_reservations{status="held"}',
        'carepoint_reservations{status="expired"}',
        "carepoint_units_available ",
    ):
        assert name in body, name


def test_production_refuses_a_short_metrics_token() -> None:
    from app.core.config import Settings

    with pytest.raises(ValueError, match="METRICS_TOKEN"):
        Settings(
            environment="production",
            jwt_secret=SecretStr("j" * 40),
            pickup_code_pepper=SecretStr("p" * 40),
            metrics_token=SecretStr("short"),
        )
