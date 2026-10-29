"""Completing a pickup: the machine has dispensed the item (ADR-013).

Phase 6 calls this when a machine reports a successful dispense. Postgres is
updated first (stock ledger + status, one transaction, reservation row locked
so the expiry sweeper skips it), then Redis drops the hold and the physical
units in one atomic step.
"""

import logging
import uuid
from datetime import UTC, datetime

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Reservation, ReservationStatus, StockMovementReason
from app.services import hold_store
from app.services.availability import ACTIVE_HOLD_STATUSES
from app.services.inventory import record_stock_movement
from app.services.payment_settlement import settle_reservation
from app.services.payments import PaymentProvider

logger = logging.getLogger(__name__)


class DispenseError(Exception):
    pass


class ReservationNotFoundError(DispenseError):
    pass


class ReservationNotDispensableError(DispenseError):
    def __init__(self, status: ReservationStatus) -> None:
        super().__init__(f"reservation is {status.value}")
        self.status = status


async def complete_dispense(
    session: AsyncSession, redis: Redis, provider: PaymentProvider, reservation_id: uuid.UUID
) -> Reservation:
    """Record a dispensed reservation. Idempotent for an already-dispensed one.

    Validity is decided once the row lock is held: a dispense that started
    while the hold was valid completes even if the clock passes `expires_at`
    while it runs, and the sweeper (SKIP LOCKED) cannot expire it meanwhile.
    If the sweeper got the lock first, the reservation is expired and the
    dispense is refused.
    """
    reservation = await session.scalar(
        select(Reservation).where(Reservation.id == reservation_id).with_for_update()
    )
    if reservation is None:
        raise ReservationNotFoundError(str(reservation_id))
    if reservation.status is ReservationStatus.DISPENSED:
        return reservation
    # Only paid reservations are dispensed (ADR-015). A held-but-unpaid one,
    # or one past its expiry, is refused.
    if reservation.status is not ReservationStatus.PAID or reservation.expires_at <= datetime.now(
        UTC
    ):
        status = reservation.status
        if status in ACTIVE_HOLD_STATUSES and reservation.expires_at <= datetime.now(UTC):
            status = ReservationStatus.EXPIRED
        raise ReservationNotDispensableError(status)

    await record_stock_movement(
        session,
        slot_id=reservation.slot_id,
        delta=-reservation.quantity,
        reason=StockMovementReason.DISPENSE,
        reservation_id=reservation.id,
    )
    reservation.status = ReservationStatus.DISPENSED
    slot_id, units = reservation.slot_id, reservation.quantity
    await session.commit()

    try:
        await hold_store.complete_dispense(redis, slot_id, reservation_id, units)
    except RedisError:
        # Redis still counts both the hold and the units, which cancel out while
        # the hold lasts; after it lapses Redis over-counts stock by `units`
        # until reconciliation (ADR-013). The ledger refuses a dispense that
        # would take stock below zero, so this cannot dispense phantom items.
        logger.exception("Could not mirror dispense of %s in Redis", reservation_id)
    # The item is out: capture now, or settlement retries until it succeeds.
    await settle_reservation(session, provider, reservation_id)
    return reservation
