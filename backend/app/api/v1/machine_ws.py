"""WebSocket transport of the machine gateway (docs/machine-protocol.md, ADR-017).

Handshake: the upgrade request is signed like any machine request (ADR-010:
method GET, empty body) and must offer the `carepoint.v1` subprotocol; a
refused handshake gets a plain HTTP 400/401/503 instead of an upgrade. After
that every frame is one JSON message, handled in order, one reply each.
"""

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, WebSocket
from fastapi.responses import JSONResponse
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import RedisClient
from app.core.config import get_settings
from app.core.db import get_session_factory
from app.models import Machine
from app.schemas.machine_protocol import MAX_MESSAGE_BYTES, SUBPROTOCOL, Welcome
from app.services import machine_gateway
from app.services.machine_auth import (
    SIGNED_HEADERS,
    MachineAuthError,
    parse_signed_headers,
    signed_path,
    verify_machine_request,
)
from app.services.machine_gateway import MachineDecommissionedError
from app.services.payments import PaymentProvider, get_payment_provider
from app.services.rate_limit import RateLimitRule

logger = logging.getLogger(__name__)

router = APIRouter(tags=["machine"])

SessionFactory = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]
Provider = Annotated[PaymentProvider, Depends(get_payment_provider)]

# Close codes (RFC 6455 and the 4000-4999 private range), see the protocol spec.
CLOSE_UNSUPPORTED_DATA = 1003  # binary frame
CLOSE_POLICY_VIOLATION = 1008  # flooding or too many invalid messages
CLOSE_TOO_BIG = 1009
CLOSE_SUPERSEDED = 4000  # the machine opened a newer connection
CLOSE_DECOMMISSIONED = 4003
CLOSE_IDLE = 4008  # nothing received within the online window


class _MessageBudget:
    """Fixed-window message counter for one connection (in memory: a connection
    lives on one worker)."""

    def __init__(self, rule: RateLimitRule) -> None:
        self._rule = rule
        self._window_start = time.monotonic()
        self._count = 0

    def spend(self) -> bool:
        now = time.monotonic()
        if now - self._window_start >= self._rule.window_seconds:
            self._window_start, self._count = now, 0
        self._count += 1
        return self._count <= self._rule.limit


async def _deny(websocket: WebSocket, status_code: int, detail: str) -> None:
    if "websocket.http.response" in websocket.scope.get("extensions", {}):
        await websocket.send_denial_response(JSONResponse({"detail": detail}, status_code))
    else:  # server without the denial extension: refused as HTTP 403
        await websocket.close(code=CLOSE_POLICY_VIOLATION)


async def _authenticate(
    websocket: WebSocket, sessions: async_sessionmaker[AsyncSession], redis: RedisClient
) -> Machine | None:
    """The signed handshake (ADR-010); denies the upgrade and returns None on failure."""
    try:
        signed = parse_signed_headers(
            {name: websocket.headers.get(name) for name in SIGNED_HEADERS}
        )
        async with sessions() as session:
            return await verify_machine_request(
                session,
                redis,
                headers=signed,
                method="GET",
                path_and_query=signed_path(websocket.scope),
                body=b"",
                max_skew_seconds=get_settings().machine_auth_max_skew_seconds,
            )
    except MachineAuthError as exc:
        logger.warning(
            "Machine gateway handshake rejected: %s (serial header=%r)",
            exc.reason,
            websocket.headers.get(SIGNED_HEADERS[0]),
        )
        await _deny(websocket, 401, "Invalid machine signature")
    except RedisError:
        logger.exception("Replay store unavailable")
        await _deny(websocket, 503, "Service unavailable")
    return None


@router.websocket("/ws/machines")
async def machine_gateway_socket(
    websocket: WebSocket, redis: RedisClient, sessions: SessionFactory, provider: Provider
) -> None:
    settings = get_settings()
    if SUBPROTOCOL not in websocket.scope.get("subprotocols", []):
        await _deny(websocket, 400, f"Subprotocol {SUBPROTOCOL} required")
        return
    machine = await _authenticate(websocket, sessions, redis)
    if machine is None:
        return

    await websocket.accept(subprotocol=SUBPROTOCOL)
    connection = await machine_gateway.open_connection(sessions, redis, provider, machine)
    try:
        await websocket.send_text(
            Welcome(
                machine_id=machine.id,
                serial_number=machine.serial_number,
                server_time=datetime.now(UTC),
                heartbeat_interval_seconds=settings.machine_heartbeat_interval_seconds,
                slots=await machine_gateway.planogram(connection),
            ).model_dump_json()
        )
        # Dispenses the machine may have missed while disconnected (ADR-018).
        for command in await machine_gateway.pending_commands(connection):
            await websocket.send_text(command.model_dump_json())
        await _serve(websocket, connection)
    finally:
        await machine_gateway.close_connection(connection)


async def _serve(websocket: WebSocket, connection: machine_gateway.MachineConnection) -> None:
    settings = get_settings()
    budget = _MessageBudget(RateLimitRule.parse(settings.machine_ws_rate_limit))
    rejected = 0
    while True:
        try:
            event = await asyncio.wait_for(
                websocket.receive(), timeout=settings.machine_online_window_seconds
            )
        except TimeoutError:
            await websocket.close(CLOSE_IDLE, "no message within the online window")
            return
        if event["type"] == "websocket.disconnect":
            return
        if not budget.spend():
            await websocket.close(CLOSE_POLICY_VIOLATION, "message rate exceeded")
            return
        text = event.get("text")
        if text is None:
            await websocket.close(CLOSE_UNSUPPORTED_DATA, "text frames only")
            return
        if len(text.encode()) > MAX_MESSAGE_BYTES:
            await websocket.close(
                CLOSE_TOO_BIG, f"messages are limited to {MAX_MESSAGE_BYTES} bytes"
            )
            return
        if not await machine_gateway.is_current(connection):
            await websocket.close(CLOSE_SUPERSEDED, "superseded by a newer connection")
            return
        try:
            outcome = await machine_gateway.handle_text(connection, text)
        except MachineDecommissionedError:
            await websocket.close(CLOSE_DECOMMISSIONED, "machine decommissioned")
            return
        await websocket.send_text(outcome.reply.model_dump_json())
        for message in outcome.follow_up:
            await websocket.send_text(message.model_dump_json())
        rejected += outcome.rejected
        if rejected >= settings.machine_ws_max_invalid_messages:
            await websocket.close(CLOSE_POLICY_VIOLATION, "too many invalid messages")
            return
