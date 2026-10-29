"""Make each order's payment follow its reservation (ADR-015).

The physical world does not wait for a payment provider: once a machine reports
"dispensed" the item is in the customer's hand, and once a hold expires the
unit is back on sale. So reservation status is decided first, and payment is
*settled* to match it:

    reservation dispensed           -> capture the authorization
    reservation expired / cancelled -> void the authorization

Cancel and dispense settle immediately (best effort); `settle_pending` runs in
the background loop and retries anything a provider outage left behind, until
payment and reservation agree. Provider capture/void are idempotent, so a retry
after an ambiguous failure is always safe.
"""

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Order, PaymentStatus, Reservation, ReservationStatus
from app.services.payments import PaymentError, PaymentProvider

logger = logging.getLogger(__name__)

CAPTURE_WHEN = (ReservationStatus.DISPENSED,)
VOID_WHEN = (ReservationStatus.EXPIRED, ReservationStatus.CANCELLED)


@dataclass
class SettlementReport:
    captured: int = 0
    voided: int = 0
    failed: int = 0


async def settle_order(
    session: AsyncSession, provider: PaymentProvider, order_id: uuid.UUID
) -> PaymentStatus | None:
    """Settle one authorized order if its reservation is final. Returns the new
    payment status, or None if there was nothing (yet) to do.

    SKIP LOCKED: if another worker is settling this order right now, leave it.
    Raises PaymentError if the provider fails; the order stays authorized and a
    later pass retries.
    """
    row = (
        await session.execute(
            select(Order, Reservation.status)
            .join(Reservation, Reservation.id == Order.reservation_id)
            .where(Order.id == order_id, Order.payment_status == PaymentStatus.AUTHORIZED)
            .with_for_update(of=Order, skip_locked=True)
        )
    ).one_or_none()
    if row is None:
        return None
    order, reservation_status = row
    if order.payment_reference is None:  # pragma: no cover - authorized orders always have one
        raise PaymentError(f"order {order.id} has no payment reference")

    if reservation_status in CAPTURE_WHEN:
        await provider.capture(order.payment_reference)
        order.payment_status = PaymentStatus.CAPTURED
    elif reservation_status in VOID_WHEN:
        await provider.void(order.payment_reference)
        order.payment_status = PaymentStatus.VOIDED
    else:
        return None  # reservation not final yet; the row lock ends with the caller's txn
    new_status = order.payment_status
    await session.commit()
    return new_status


async def settle_reservation(
    session: AsyncSession, provider: PaymentProvider, reservation_id: uuid.UUID
) -> PaymentStatus | None:
    """Best-effort immediate settlement after a cancel or dispense."""
    order_id = await session.scalar(select(Order.id).where(Order.reservation_id == reservation_id))
    if order_id is None:
        return None
    try:
        return await settle_order(session, provider, order_id)
    except PaymentError:
        # Nothing was modified (the provider call comes first); retried later.
        logger.warning("Settlement of reservation %s deferred to retry", reservation_id)
        return None


async def settle_pending(
    session: AsyncSession, provider: PaymentProvider, *, batch_size: int = 100
) -> SettlementReport:
    """Retry loop body: settle every authorized order whose reservation is final."""
    report = SettlementReport()
    order_ids = list(
        await session.scalars(
            select(Order.id)
            .join(Reservation, Reservation.id == Order.reservation_id)
            .where(
                Order.payment_status == PaymentStatus.AUTHORIZED,
                Reservation.status.in_(CAPTURE_WHEN + VOID_WHEN),
            )
            .order_by(Order.updated_at)
            .limit(batch_size)
        )
    )
    for order_id in order_ids:  # each successful settlement commits on its own
        try:
            status = await settle_order(session, provider, order_id)
        except PaymentError:
            report.failed += 1
            continue
        if status is PaymentStatus.CAPTURED:
            report.captured += 1
        elif status is PaymentStatus.VOIDED:
            report.voided += 1
    if report.failed:
        logger.warning("Payment settlement: %s", report)
    return report
