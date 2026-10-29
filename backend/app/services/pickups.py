"""Pickup at the machine: an accepted code becomes a dispense command, and the
machine's result completes the reservation (ADR-018).

    verify_code -> request_pickup()        -> `dispense` command (status open)
    dispense_result -> record_dispense_result() -> dispensed + captured, or
                                               retryable, partial or late

Lock order everywhere is the reservation row, then its dispense command: the
same order as the expiry sweeper and cancellation, so none of them deadlock.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import (
    DispenseCommand,
    DispenseCommandStatus,
    Machine,
    MachineErrorCode,
    MachineSlot,
    MachineStatus,
    Reservation,
    ReservationStatus,
    StockMovementReason,
)
from app.services import hold_store, pickup_codes
from app.services.dispensing import complete_dispense
from app.services.inventory import InsufficientStockError, record_stock_movement
from app.services.payment_settlement import settle_reservation
from app.services.payments import PaymentProvider
from app.services.pickup_codes import InvalidPickupCodeError, PickupError, is_pickupable
from app.services.reservations import load_slot_state

logger = logging.getLogger(__name__)


class MachineUnavailableError(PickupError):
    """The machine is not `active` (maintenance), so it does not dispense."""


class PickupInProgressError(PickupError):
    """This reservation is already being dispensed."""


class DispenseResultError(Exception):
    """A dispense result that cannot be applied; nothing was changed."""


class UnknownCommandError(DispenseResultError):
    pass


class InconsistentResultError(DispenseResultError):
    pass


@dataclass(frozen=True)
class IssuedDispense:
    command_id: uuid.UUID
    reservation_id: uuid.UUID
    slot_code: str
    quantity: int


async def _open_command_id(session: AsyncSession, reservation_id: uuid.UUID) -> uuid.UUID | None:
    command_id: uuid.UUID | None = await session.scalar(
        select(DispenseCommand.id).where(
            DispenseCommand.reservation_id == reservation_id,
            DispenseCommand.status == DispenseCommandStatus.OPEN,
        )
    )
    return command_id


async def is_being_dispensed(session: AsyncSession, reservation_id: uuid.UUID) -> bool:
    return await _open_command_id(session, reservation_id) is not None


async def request_pickup(
    session: AsyncSession, redis: Redis, machine_id: uuid.UUID, credential: str
) -> IssuedDispense:
    """Turn a code typed or scanned at `machine_id` into a dispense command.

    The hold is extended to at least now + DISPENSE_TIMEOUT_SECONDS first, in
    Redis and then Postgres, so a slow dispense can never race the expiry: the
    units cannot go back on sale while they are coming out of the machine.
    """
    machine = await session.get(Machine, machine_id)
    if machine is None or machine.status is not MachineStatus.ACTIVE:
        raise MachineUnavailableError("machine is not in service")
    verified = await pickup_codes.verify(session, redis, machine, credential)

    reservation = await session.scalar(
        select(Reservation)
        .where(Reservation.id == verified.reservation.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    # Re-checked under the lock: it may have expired or been cancelled since.
    if reservation is None or not is_pickupable(reservation):
        raise InvalidPickupCodeError("invalid code")
    if await is_being_dispensed(session, reservation.id):
        raise PickupInProgressError("this pickup is already being dispensed")

    deadline = datetime.now(UTC) + timedelta(seconds=get_settings().dispense_timeout_seconds)
    if deadline > reservation.expires_at:
        # Redis first: if Postgres then fails, Redis merely holds the units
        # longer than needed (safe) until the sweeper or reconciliation tidies up.
        await hold_store.extend(
            redis,
            reservation.slot_id,
            reservation.id,
            reservation.quantity,
            deadline,
            partial(load_slot_state, session),
        )
        reservation.expires_at = deadline
    command = DispenseCommand(
        machine_id=machine_id,
        reservation_id=reservation.id,
        slot_id=reservation.slot_id,
        quantity=reservation.quantity,
    )
    session.add(command)
    await session.commit()
    logger.info("Dispense %s issued for reservation %s", command.id, reservation.id)
    return IssuedDispense(command.id, reservation.id, verified.slot_code, reservation.quantity)


async def commands_to_resend(session: AsyncSession, machine_id: uuid.UUID) -> list[IssuedDispense]:
    """Open commands a (re)connecting machine must get again, oldest first: the
    machine may never have received them. Only while the reservation is still
    pickupable; after that nobody is waiting at the machine."""
    rows = await session.execute(
        select(DispenseCommand, MachineSlot.slot_code)
        .join(Reservation, Reservation.id == DispenseCommand.reservation_id)
        .join(MachineSlot, MachineSlot.id == DispenseCommand.slot_id)
        .where(
            DispenseCommand.machine_id == machine_id,
            DispenseCommand.status == DispenseCommandStatus.OPEN,
            Reservation.status == ReservationStatus.PAID,
            Reservation.expires_at > func.now(),
        )
        .order_by(DispenseCommand.created_at, DispenseCommand.id)
    )
    return [
        IssuedDispense(command.id, command.reservation_id, slot_code, command.quantity)
        for command, slot_code in rows
    ]


async def slots_in_flight(session: AsyncSession, slot_ids: list[uuid.UUID]) -> set[uuid.UUID]:
    """Slots with a dispense under way, whose physical count is in motion."""
    return set(
        await session.scalars(
            select(DispenseCommand.slot_id).where(
                DispenseCommand.slot_id.in_(slot_ids),
                DispenseCommand.status == DispenseCommandStatus.OPEN,
            )
        )
    )


async def record_dispense_result(
    session: AsyncSession,
    redis: Redis,
    provider: PaymentProvider,
    machine_id: uuid.UUID,
    command_id: uuid.UUID,
    *,
    dispensed: bool,
    units_dispensed: int,
    error_code: MachineErrorCode | None,
) -> None:
    """Apply what the machine says happened. Idempotent: a resent result for a
    finished command changes nothing.

    - all units out, reservation still pickupable -> dispensed, payment captured;
    - nothing out -> command failed, reservation stays paid (the customer may
      try again until the hold expires, then the payment is voided);
    - some units out -> reservation cancelled and payment voided (the customer
      is never charged for an incomplete order), units out recorded;
    - result after the reservation expired -> units out recorded, nobody charged.
    """
    reservation_id = await session.scalar(
        select(DispenseCommand.reservation_id).where(
            DispenseCommand.id == command_id, DispenseCommand.machine_id == machine_id
        )
    )
    if reservation_id is None:
        raise UnknownCommandError(f"no such command: {command_id}")
    reservation = await session.scalar(
        select(Reservation)
        .where(Reservation.id == reservation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    command = await session.scalar(
        select(DispenseCommand)
        .where(DispenseCommand.id == command_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if reservation is None or command is None:  # deleted meanwhile; foreign keys forbid it
        raise UnknownCommandError(f"no such command: {command_id}")

    quantity = command.quantity
    if dispensed and units_dispensed != quantity:
        raise InconsistentResultError(f"dispensed must report all {quantity} units")
    if not dispensed and units_dispensed >= quantity:
        raise InconsistentResultError(f"failed must report fewer than {quantity} units")
    if command.status in (DispenseCommandStatus.SUCCEEDED, DispenseCommandStatus.FAILED):
        return  # a resent result: applied the first time

    was_open = command.status is DispenseCommandStatus.OPEN
    command.status = DispenseCommandStatus.SUCCEEDED if dispensed else DispenseCommandStatus.FAILED
    command.units_dispensed = units_dispensed
    command.error_code = error_code
    command.completed_at = datetime.now(UTC)

    if not (was_open and is_pickupable(reservation)):
        await _record_late(session, redis, command, reservation)
    elif dispensed:
        await complete_dispense(session, redis, provider, reservation.id)  # commits the command too
        logger.info("Dispense %s completed reservation %s", command.id, reservation.id)
    elif units_dispensed == 0:
        await session.commit()
        logger.warning(
            "Dispense %s failed (%s); reservation %s stays paid",
            command.id,
            error_code,
            reservation.id,
        )
    else:
        await _record_partial(session, redis, provider, command, reservation)


async def _units_out(session: AsyncSession, command: DispenseCommand, note: str) -> int:
    """Record the units that physically left the slot; returns how many were
    recorded (0 if the database already counts fewer, which the machine's next
    stock report corrects)."""
    units = command.units_dispensed or 0
    if units == 0:
        return 0
    try:
        await record_stock_movement(
            session,
            slot_id=command.slot_id,
            delta=-units,
            reason=StockMovementReason.DISPENSE,
            reservation_id=command.reservation_id,
            note=note,
        )
    except InsufficientStockError:
        logger.warning("Slot %s has fewer than %d units on record", command.slot_id, units)
        return 0
    return units


async def _record_partial(
    session: AsyncSession,
    redis: Redis,
    provider: PaymentProvider,
    command: DispenseCommand,
    reservation: Reservation,
) -> None:
    units = await _units_out(
        session,
        command,
        f"partial dispense: {command.units_dispensed} of {command.quantity} units; "
        "reservation cancelled and payment voided",
    )
    reservation.status = ReservationStatus.CANCELLED
    await session.commit()
    logger.warning(
        "Dispense %s partial (%s of %s); reservation %s cancelled",
        command.id,
        command.units_dispensed,
        command.quantity,
        reservation.id,
    )
    try:
        # Drop the hold and the units that came out in one atomic step.
        await hold_store.complete_dispense(redis, command.slot_id, reservation.id, units)
    except RedisError:
        logger.exception("Could not mirror partial dispense %s in Redis", command.id)
    await settle_reservation(session, provider, reservation.id)  # void


async def _record_late(
    session: AsyncSession, redis: Redis, command: DispenseCommand, reservation: Reservation
) -> None:
    """The result arrived after the reservation stopped being pickupable (the
    hold expired, so its payment is voided). Whatever came out is recorded; the
    customer is not charged."""
    units = await _units_out(
        session,
        command,
        f"late dispense result; reservation was {reservation.status.value}, not charged",
    )
    await session.commit()
    logger.warning(
        "Late result for dispense %s (reservation %s %s): %d unit(s) out",
        command.id,
        reservation.id,
        reservation.status.value,
        units,
    )
    if units:
        try:
            # The hold is already gone (released, or lapsed in Redis).
            await hold_store.apply_stock_delta(redis, command.slot_id, -units)
        except RedisError:
            logger.exception("Could not mirror late dispense %s in Redis", command.id)
