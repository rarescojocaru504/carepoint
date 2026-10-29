"""Machine gateway: one live connection per machine, and what to do with each
message it sends (ADR-017). The WebSocket transport is in api/v1/machine_ws.py.

Every message is handled in its own short database session: a connection
stays open for hours, and holding one session (and its identity map) for that
long would serve stale rows and pin a pooled connection.
"""

import logging
import uuid
from dataclasses import dataclass, field

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Machine, MachineEventKind, MachineSlot, MachineStatus, Product
from app.schemas.machine_protocol import (
    Ack,
    Dispense,
    DispenseResult,
    Error,
    ErrorEvent,
    Heartbeat,
    InvalidMessageError,
    MachineMessage,
    PickupRefusal,
    PlanogramSlot,
    ProtocolErrorCode,
    ServerMessage,
    StockReport,
    Telemetry,
    VerifyCode,
    VerifyResult,
    parse_machine_message,
)
from app.services import events, machine_telemetry, pickups
from app.services.events import Topic
from app.services.machine_telemetry import StockReportError
from app.services.payments import PaymentProvider
from app.services.pickup_codes import InvalidPickupCodeError, TooManyAttemptsError
from app.services.pickups import (
    IssuedDispense,
    MachineUnavailableError,
    PickupInProgressError,
    UnknownCommandError,
)

logger = logging.getLogger(__name__)

# Newest connection per machine. A connection that finds it is no longer the
# newest is closed, so a machine that reconnected before its old socket timed
# out is never served on two connections at once (even on different workers).
_CONNECTION_KEY = "machine-connection:{machine_id}"
_CONNECTION_TTL_SECONDS = 24 * 3600

# Server-initiated commands for a machine are published here; whichever worker
# holds the machine's connection forwards them (ADR-021).
_COMMAND_CHANNEL = "cp:machine-commands:{machine_id}"


def command_channel(machine_id: uuid.UUID) -> str:
    return _COMMAND_CHANNEL.format(machine_id=machine_id)


async def send_command(redis: Redis, machine_id: uuid.UUID, message: ServerMessage) -> bool:
    """Deliver a command to the machine's live connection, if it has one.
    Returns whether any connection received it."""
    receivers: int = await redis.publish(command_channel(machine_id), message.model_dump_json())
    return receivers > 0


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
    provider: PaymentProvider


@dataclass(frozen=True)
class Outcome:
    reply: ServerMessage
    rejected: bool  # counts towards the connection's invalid-message limit
    follow_up: list[ServerMessage] = field(default_factory=list)  # sent after the reply


def dispense_message(issued: IssuedDispense) -> Dispense:
    return Dispense(
        command_id=issued.command_id,
        reservation_id=issued.reservation_id,
        slot=issued.slot_code,
        quantity=issued.quantity,
    )


async def open_connection(
    sessions: async_sessionmaker[AsyncSession],
    redis: Redis,
    provider: PaymentProvider,
    machine: Machine,
) -> MachineConnection:
    connection = MachineConnection(machine.id, uuid.uuid4().hex, sessions, redis, provider)
    await redis.set(
        _CONNECTION_KEY.format(machine_id=machine.id),
        connection.connection_id,
        ex=_CONNECTION_TTL_SECONDS,
    )
    async with sessions() as session:
        await machine_telemetry.record_connection(session, machine.id, MachineEventKind.CONNECTED)
    logger.info("Machine %s connected (%s)", machine.serial_number, connection.connection_id)
    await events.publish(redis, Topic.MACHINE, machine_id=machine.id, change="connected")
    return connection


async def planogram(connection: MachineConnection) -> list[PlanogramSlot]:
    """The slot layout sent in `welcome`, so a machine (or a fresh simulator)
    knows which product belongs where without any local configuration."""
    async with connection.sessions() as session:
        rows = await session.execute(
            select(MachineSlot, Product.sku, Product.name)
            .outerjoin(Product, Product.id == MachineSlot.product_id)
            .where(MachineSlot.machine_id == connection.machine_id)
            .order_by(MachineSlot.slot_code)
        )
        return [
            PlanogramSlot(
                slot=slot.slot_code,
                product_sku=sku,
                product_name=name,
                capacity=slot.capacity,
                quantity=slot.quantity,
            )
            for slot, sku, name in rows
        ]


async def pending_commands(connection: MachineConnection) -> list[Dispense]:
    """Commands to (re)send after `welcome`: the machine may have missed them."""
    async with connection.sessions() as session:
        issued = await pickups.commands_to_resend(session, connection.machine_id)
    return [dispense_message(command) for command in issued]


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
    newest = True  # if Redis cannot tell, mark offline; the next heartbeat corrects it
    try:
        newest = bool(
            await connection.redis.register_script(_RELEASE_SCRIPT)(
                keys=[_CONNECTION_KEY.format(machine_id=connection.machine_id)],
                args=[connection.connection_id],
            )
        )
    except RedisError:
        logger.warning("Could not release connection key of %s", connection.machine_id)
    try:
        async with connection.sessions() as session:
            await machine_telemetry.record_connection(
                session, connection.machine_id, MachineEventKind.DISCONNECTED, offline=newest
            )
    except Exception:
        logger.exception("Could not record disconnect of machine %s", connection.machine_id)
    logger.info("Machine %s disconnected (%s)", connection.machine_id, connection.connection_id)
    if newest:
        await events.publish(
            connection.redis, Topic.MACHINE, machine_id=connection.machine_id, change="disconnected"
        )


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
    machine_id, redis = connection.machine_id, connection.redis
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
            await events.publish(redis, Topic.MACHINE, machine_id=machine_id, change="telemetry")
        case ErrorEvent():
            await machine_telemetry.record_error(
                session,
                machine_id,
                code=message.code,
                slot_code=message.slot,
                message=message.message,
            )
            await events.publish(redis, Topic.MACHINE, machine_id=machine_id, change="fault")
        case StockReport():
            try:
                corrections = await machine_telemetry.reconcile_stock_report(
                    session,
                    connection.redis,
                    machine_id,
                    {count.slot: count.quantity for count in message.slots},
                )
            except StockReportError as exc:
                return Outcome(
                    Error(reply_to=message.id, code=exc.code, detail=exc.detail), rejected=True
                )
            if corrections:
                await events.publish(redis, Topic.STOCK, machine_id=machine_id, change="corrected")
        case VerifyCode():
            return await _verify(connection, session, message)
        case DispenseResult():
            try:
                await pickups.record_dispense_result(
                    session,
                    connection.redis,
                    connection.provider,
                    machine_id,
                    message.command_id,
                    dispensed=message.outcome == "dispensed",
                    units_dispensed=message.units_dispensed,
                    error_code=message.error_code,
                )
            except UnknownCommandError as exc:
                error = Error(
                    reply_to=message.id, code=ProtocolErrorCode.UNKNOWN_COMMAND, detail=str(exc)
                )
                return Outcome(error, rejected=True)
            except pickups.InconsistentResultError as exc:
                error = Error(
                    reply_to=message.id, code=ProtocolErrorCode.OUT_OF_RANGE, detail=str(exc)
                )
                return Outcome(error, rejected=True)
    return Outcome(Ack(reply_to=message.id), rejected=False)


async def _verify(
    connection: MachineConnection, session: AsyncSession, message: VerifyCode
) -> Outcome:
    """A wrong code is the customer's mistake, not the machine's: it is refused
    in `verify_result` and never counts towards the invalid-message limit (the
    per-machine wrong-code lockout handles guessing, ADR-016)."""

    def refused(refusal: PickupRefusal, retry_after: int | None = None) -> Outcome:
        result = VerifyResult(
            reply_to=message.id, ok=False, refusal=refusal, retry_after_seconds=retry_after
        )
        return Outcome(result, rejected=False)

    try:
        issued = await pickups.request_pickup(
            session, connection.redis, connection.machine_id, message.credential
        )
    except TooManyAttemptsError as exc:
        return refused(PickupRefusal.TOO_MANY_ATTEMPTS, exc.retry_after_seconds)
    except InvalidPickupCodeError:
        return refused(PickupRefusal.INVALID_CODE)
    except PickupInProgressError:
        return refused(PickupRefusal.PICKUP_IN_PROGRESS)
    except MachineUnavailableError:
        return refused(PickupRefusal.MACHINE_UNAVAILABLE)
    result = VerifyResult(reply_to=message.id, ok=True, command_id=issued.command_id)
    return Outcome(result, rejected=False, follow_up=[dispense_message(issued)])
