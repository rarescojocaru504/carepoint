"""Machine gateway: one live connection per machine, and what to do with each
message it sends (ADR-017). The WebSocket transport is in api/v1/machine_ws.py.

Every message is handled in its own short database session: a connection
stays open for hours, and holding one session (and its identity map) for that
long would serve stale rows and pin a pooled connection.
"""

import logging
import uuid
from dataclasses import dataclass

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Machine, MachineEventKind, MachineStatus
from app.schemas.machine_protocol import (
    Ack,
    Error,
    ErrorEvent,
    Heartbeat,
    InvalidMessageError,
    MachineMessage,
    ProtocolErrorCode,
    ServerMessage,
    StockReport,
    Telemetry,
    parse_machine_message,
)
from app.services import machine_telemetry
from app.services.machine_telemetry import StockReportError

logger = logging.getLogger(__name__)

# Newest connection per machine. A connection that finds it is no longer the
# newest is closed, so a machine that reconnected before its old socket timed
# out is never served on two connections at once (even on different workers).
_CONNECTION_KEY = "machine-connection:{machine_id}"
_CONNECTION_TTL_SECONDS = 24 * 3600

# Delete the key only if it still names this connection.
_RELEASE_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end
return 0
"""


class MachineDecommissionedError(Exception):
    """Operators decommissioned the machine while it was connected."""


@dataclass(frozen=True)
class MachineConnection:
    machine_id: uuid.UUID
    connection_id: str
    sessions: async_sessionmaker[AsyncSession]
    redis: Redis


@dataclass(frozen=True)
class Outcome:
    reply: ServerMessage
    rejected: bool  # counts towards the connection's invalid-message limit


async def open_connection(
    sessions: async_sessionmaker[AsyncSession], redis: Redis, machine: Machine
) -> MachineConnection:
    connection = MachineConnection(machine.id, uuid.uuid4().hex, sessions, redis)
    await redis.set(
        _CONNECTION_KEY.format(machine_id=machine.id),
        connection.connection_id,
        ex=_CONNECTION_TTL_SECONDS,
    )
    async with sessions() as session:
        await machine_telemetry.record_connection(session, machine.id, MachineEventKind.CONNECTED)
    logger.info("Machine %s connected (%s)", machine.serial_number, connection.connection_id)
    return connection


async def is_current(connection: MachineConnection) -> bool:
    try:
        newest = await connection.redis.get(
            _CONNECTION_KEY.format(machine_id=connection.machine_id)
        )
    except RedisError:
        logger.warning("Could not check connection %s; assuming current", connection.connection_id)
        return True
    # A missing key (Redis restarted) cannot name a newer connection.
    return newest is None or newest == connection.connection_id


async def close_connection(connection: MachineConnection) -> None:
    """Best effort: a failure here must not mask why the connection ended."""
    try:
        await connection.redis.register_script(_RELEASE_SCRIPT)(
            keys=[_CONNECTION_KEY.format(machine_id=connection.machine_id)],
            args=[connection.connection_id],
        )
    except RedisError:
        logger.warning("Could not release connection key of %s", connection.machine_id)
    try:
        async with connection.sessions() as session:
            await machine_telemetry.record_connection(
                session, connection.machine_id, MachineEventKind.DISCONNECTED
            )
    except Exception:
        logger.exception("Could not record disconnect of machine %s", connection.machine_id)
    logger.info("Machine %s disconnected (%s)", connection.machine_id, connection.connection_id)


async def handle_text(connection: MachineConnection, text: str) -> Outcome:
    """Parse, validate and apply one message; return the reply to send."""
    try:
        message = parse_machine_message(text)
    except InvalidMessageError as exc:
        error = Error(
            reply_to=exc.reply_to, code=ProtocolErrorCode.INVALID_MESSAGE, detail=exc.detail
        )
        return Outcome(error, rejected=True)
    try:
        async with connection.sessions() as session:
            status = await session.scalar(
                select(Machine.status).where(Machine.id == connection.machine_id)
            )
            if status is None or status is MachineStatus.DECOMMISSIONED:
                raise MachineDecommissionedError(str(connection.machine_id))
            return await _dispatch(connection, session, message)
    except MachineDecommissionedError:
        raise
    except Exception:
        # Not the machine's fault (database or Redis trouble): it may retry.
        logger.exception("Failed to handle %s from machine %s", message.type, connection.machine_id)
        error = Error(
            reply_to=message.id,
            code=ProtocolErrorCode.INTERNAL_ERROR,
            detail="internal error, retry later",
        )
        return Outcome(error, rejected=False)


async def _dispatch(
    connection: MachineConnection, session: AsyncSession, message: MachineMessage
) -> Outcome:
    machine_id = connection.machine_id
    match message:
        case Heartbeat():
            await machine_telemetry.record_heartbeat(session, machine_id, message.firmware_version)
        case Telemetry():
            await machine_telemetry.record_telemetry(
                session,
                machine_id,
                door_open=message.door == "open",
                temperature_c=message.temperature_c,
            )
        case ErrorEvent():
            await machine_telemetry.record_error(
                session,
                machine_id,
                code=message.code,
                slot_code=message.slot,
                message=message.message,
            )
        case StockReport():
            try:
                await machine_telemetry.reconcile_stock_report(
                    session,
                    connection.redis,
                    machine_id,
                    {count.slot: count.quantity for count in message.slots},
                )
            except StockReportError as exc:
                return Outcome(
                    Error(reply_to=message.id, code=exc.code, detail=exc.detail), rejected=True
                )
    return Outcome(Ack(reply_to=message.id), rejected=False)
