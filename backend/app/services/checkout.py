"""Checkout: pay for a held reservation (held -> paid). ADR-015."""

import uuid
from dataclasses import dataclass

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Order, PaymentStatus, Product, Reservation, ReservationStatus, User
from app.models.machine import MachineSlot
from app.services import events
from app.services.events import Topic
from app.services.payments import PaymentProvider
from app.services.pickup_codes import PickupCredential, assign_code
from app.services.reservations import (
    ReservationError,
    ReservationNotFoundError,
    effective_status,
)


class ReservationNotPayableError(ReservationError):
    def __init__(self, status: ReservationStatus) -> None:
        super().__init__(f"reservation is {status.value}")
        self.status = status


@dataclass(frozen=True)
class CheckoutResult:
    reservation: Reservation
    order: Order
    created: bool  # False for an idempotent repeat
    # The plaintext code exists only here, at issue time (only its hash is
    # stored), so an idempotent repeat cannot return it: rotate instead.
    pickup: PickupCredential | None


async def checkout(
    session: AsyncSession,
    redis: Redis,
    provider: PaymentProvider,
    user: User,
    reservation_id: uuid.UUID,
    payment_method: str,
) -> CheckoutResult:
    """Authorize payment for a held reservation and mark it paid.

    The reservation row is locked for the duration, so a concurrent checkout
    waits and then sees `paid` (returning the same order), and the expiry
    sweeper (SKIP LOCKED) cannot expire it mid-checkout. Validity is judged
    once the lock is held.

    The provider idempotency key is derived from the reservation, so if the
    process dies after authorizing but before committing, a retry gets the
    same authorization instead of a second one.
    """
    reservation = await session.scalar(
        select(Reservation)
        .where(Reservation.id == reservation_id, Reservation.user_id == user.id)
        .with_for_update()
    )
    if reservation is None:
        raise ReservationNotFoundError("reservation not found")

    if reservation.status is ReservationStatus.PAID:
        existing = await session.scalar(select(Order).where(Order.reservation_id == reservation.id))
        if existing is not None:
            return CheckoutResult(reservation, existing, created=False, pickup=None)

    status = effective_status(reservation)
    if status is not ReservationStatus.HELD:
        raise ReservationNotPayableError(status)

    currency = (
        await session.execute(
            select(Product.currency)
            .join(MachineSlot, MachineSlot.product_id == Product.id)
            .where(MachineSlot.id == reservation.slot_id)
        )
    ).scalar_one()  # every reservable slot has a product
    amount = reservation.unit_price_cents * reservation.quantity
    # Declines and outages raise here, before anything is written: the
    # reservation stays held and the customer can retry until it expires.
    authorization = await provider.authorize(
        amount_cents=amount,
        currency=currency,
        payment_method=payment_method,
        idempotency_key=f"checkout:{reservation.id}",
    )
    order = Order(
        reservation_id=reservation.id,
        user_id=user.id,
        amount_cents=amount,
        currency=currency,
        payment_status=PaymentStatus.AUTHORIZED,
        payment_reference=authorization.reference,
    )
    session.add(order)
    reservation.status = ReservationStatus.PAID
    await session.flush()
    pickup = await assign_code(session, reservation)
    await session.commit()
    await session.refresh(order)
    await events.publish_for_slot(session, redis, Topic.RESERVATION, reservation.slot_id,
                                  change="paid")  # fmt: skip
    return CheckoutResult(reservation, order, created=True, pickup=pickup)
