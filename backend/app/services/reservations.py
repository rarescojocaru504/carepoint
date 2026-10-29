"""Reservations: hold stock for a customer for a limited time (ADR-012)."""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import (
    DispenseCommand,
    DispenseCommandStatus,
    Machine,
    MachineSlot,
    MachineStatus,
    Product,
    Reservation,
    ReservationStatus,
    User,
)
from app.schemas.reservation import ReservationCreate
from app.services import events, hold_store
from app.services.availability import ACTIVE_HOLD_STATUSES
from app.services.events import Topic
from app.services.payment_settlement import settle_reservation
from app.services.payments import PaymentProvider

logger = logging.getLogger(__name__)


class ReservationError(Exception):
    """Base class; messages are safe to show to clients."""


class SlotNotFoundError(ReservationError):
    pass


class SlotNotSellableError(ReservationError):
    """The slot exists but cannot be reserved now (machine or product unavailable)."""


class InsufficientStockError(ReservationError):
    def __init__(self, available: int) -> None:
        super().__init__(f"only {available} available")
        self.available = available


class TooManyActiveReservationsError(ReservationError):
    def __init__(self, limit: int) -> None:
        super().__init__(f"at most {limit} active reservations")
        self.limit = limit


class IdempotencyConflictError(ReservationError):
    """The idempotency key was already used for a different request."""


class ReservationNotFoundError(ReservationError):
    pass


class ReservationNotCancellableError(ReservationError):
    def __init__(self, status: ReservationStatus) -> None:
        super().__init__(f"reservation is {status.value}")
        self.status = status


class ReservationBeingDispensedError(ReservationError):
    """The machine is dispensing it; cancelling now would void a delivered order."""


@dataclass(frozen=True)
class ReservationView:
    reservation: Reservation
    machine_id: uuid.UUID
    product_sku: str
    product_name: str
    currency: str


def effective_status(reservation: Reservation, now: datetime | None = None) -> ReservationStatus:
    """A hold past its expiry is expired, even before the sweeper records it."""
    now = now or datetime.now(UTC)
    if reservation.status in ACTIVE_HOLD_STATUSES and reservation.expires_at <= now:
        return ReservationStatus.EXPIRED
    return reservation.status


def _user_lock_key(user_id: uuid.UUID) -> int:
    # pg_advisory_xact_lock takes a signed 64-bit key.
    return int.from_bytes(user_id.bytes[:8], "big", signed=True)


async def load_slot_state(session: AsyncSession, slot_id: uuid.UUID) -> hold_store.SlotState:
    """Postgres's view of a slot, used to (re)build its Redis state."""
    quantity = await session.scalar(select(MachineSlot.quantity).where(MachineSlot.id == slot_id))
    rows = await session.execute(
        select(Reservation.id, Reservation.quantity, Reservation.expires_at).where(
            Reservation.slot_id == slot_id,
            Reservation.status.in_(ACTIVE_HOLD_STATUSES),
            Reservation.expires_at > func.now(),
        )
    )
    return hold_store.SlotState(
        physical_quantity=quantity or 0,
        holds=[hold_store.ExistingHold(rid, units, expires) for rid, units, expires in rows],
    )


async def _view(session: AsyncSession, reservation: Reservation) -> ReservationView:
    machine_id, sku, name, currency = (
        await session.execute(
            select(MachineSlot.machine_id, Product.sku, Product.name, Product.currency)
            .join(Product, Product.id == MachineSlot.product_id)
            .where(MachineSlot.id == reservation.slot_id)
        )
    ).one()
    return ReservationView(reservation, machine_id, sku, name, currency)


async def _existing_for_key(
    session: AsyncSession, user: User, data: ReservationCreate
) -> Reservation | None:
    if data.idempotency_key is None:
        return None
    existing = await session.scalar(
        select(Reservation).where(
            Reservation.user_id == user.id, Reservation.idempotency_key == data.idempotency_key
        )
    )
    if existing is not None and (
        existing.slot_id != data.slot_id or existing.quantity != data.quantity
    ):
        raise IdempotencyConflictError("idempotency key reused with different parameters")
    return existing


async def create_reservation(
    session: AsyncSession, redis: Redis, user: User, data: ReservationCreate
) -> tuple[ReservationView, bool]:
    """Hold stock for `user`. Returns (reservation, created).

    `created` is False when an idempotent retry returned the existing reservation.
    """
    settings = get_settings()

    # An idempotent retry gets its original reservation, even if the item has
    # sold out since, so this comes before the fast-fail below.
    if (existing := await _existing_for_key(session, user, data)) is not None:
        return await _view(session, existing), False

    # Fast fail: in a rush almost every request loses. Redis already knows the
    # slot is short, so refuse before taking locks or querying Postgres. Not a
    # correctness check (an atomic Lua script still decides every request that
    # gets past this); it only spares the database the work of losers.
    snapshot = await hold_store.snapshot(redis, data.slot_id)
    if snapshot.initialised and snapshot.available < data.quantity:
        raise InsufficientStockError(snapshot.available)

    # Serialise this user's reservation requests: the active-hold limit and the
    # idempotency check are then race-free. Released at commit/rollback.
    await session.execute(select(func.pg_advisory_xact_lock(_user_lock_key(user.id))))
    if (existing := await _existing_for_key(session, user, data)) is not None:
        return await _view(session, existing), False  # a concurrent duplicate won

    row = (
        await session.execute(
            select(MachineSlot, Machine.status, Product)
            .join(Machine, Machine.id == MachineSlot.machine_id)
            .outerjoin(Product, Product.id == MachineSlot.product_id)
            .where(MachineSlot.id == data.slot_id)
        )
    ).one_or_none()
    if row is None:
        raise SlotNotFoundError("slot not found")
    slot, machine_status, product = row
    if machine_status is MachineStatus.DECOMMISSIONED:
        raise SlotNotFoundError("slot not found")
    if product is None or not product.is_active or machine_status is not MachineStatus.ACTIVE:
        raise SlotNotSellableError("this item cannot be reserved right now")

    active = await session.scalar(
        select(func.count())
        .select_from(Reservation)
        .where(
            Reservation.user_id == user.id,
            Reservation.status.in_(ACTIVE_HOLD_STATUSES),
            Reservation.expires_at > func.now(),
        )
    )
    if (active or 0) >= settings.max_active_reservations_per_user:
        raise TooManyActiveReservationsError(settings.max_active_reservations_per_user)

    reservation_id = uuid.uuid4()
    result = await hold_store.reserve(
        redis,
        slot.id,
        reservation_id,
        data.quantity,
        settings.reservation_hold_seconds,
        lambda slot_id: load_slot_state(session, slot_id),
    )
    if not result.granted or result.expires_at is None:
        raise InsufficientStockError(result.available)

    reservation = Reservation(
        id=reservation_id,
        user_id=user.id,
        slot_id=slot.id,
        quantity=data.quantity,
        unit_price_cents=product.price_cents,
        status=ReservationStatus.HELD,
        expires_at=result.expires_at,
        idempotency_key=data.idempotency_key,
    )
    session.add(reservation)
    try:
        await session.commit()
    except BaseException:
        # Redis granted the hold but Postgres did not record it: give it back.
        await session.rollback()
        await _release_quietly(redis, slot.id, reservation_id)
        raise
    await session.refresh(reservation)  # load server-generated timestamps
    view = ReservationView(
        reservation, slot.machine_id, product.sku, product.name, product.currency
    )
    await events.publish(redis, Topic.RESERVATION, machine_id=slot.machine_id, change="created")
    return view, True


async def _release_quietly(redis: Redis, slot_id: uuid.UUID, reservation_id: uuid.UUID) -> None:
    """Best effort: if Redis is unreachable the hold still lapses at its expiry."""
    try:
        await hold_store.release(redis, slot_id, reservation_id)
    except RedisError:
        logger.exception("Could not release hold %s; it will lapse at expiry", reservation_id)


async def get_reservation(
    session: AsyncSession, user: User, reservation_id: uuid.UUID
) -> ReservationView:
    """A user's own reservation. Someone else's is reported as not found."""
    reservation = await session.scalar(
        select(Reservation).where(Reservation.id == reservation_id, Reservation.user_id == user.id)
    )
    if reservation is None:
        raise ReservationNotFoundError("reservation not found")
    return await _view(session, reservation)


async def list_reservations(
    session: AsyncSession, user: User, *, limit: int = 20
) -> list[ReservationView]:
    reservations = await session.scalars(
        select(Reservation)
        .where(Reservation.user_id == user.id)
        .order_by(Reservation.created_at.desc(), Reservation.id)
        .limit(limit)
    )
    return [await _view(session, r) for r in reservations]


async def cancel_reservation(
    session: AsyncSession,
    redis: Redis,
    provider: PaymentProvider,
    user: User,
    reservation_id: uuid.UUID,
) -> None:
    """Cancel a held or paid reservation, release its stock and void any
    payment. Idempotent.

    Postgres is updated first, then Redis: if the Redis release fails, the hold
    keeps the unit locked only until its expiry (safe), never the reverse. The
    void is attempted immediately and retried by settlement if it fails.
    """
    await _cancel(session, redis, provider, reservation_id, owner_id=user.id)


async def cancel_any_reservation(
    session: AsyncSession, redis: Redis, provider: PaymentProvider, reservation_id: uuid.UUID
) -> None:
    """An operator cancels a customer's reservation (same rules as the customer)."""
    await _cancel(session, redis, provider, reservation_id, owner_id=None)


async def _cancel(
    session: AsyncSession,
    redis: Redis,
    provider: PaymentProvider,
    reservation_id: uuid.UUID,
    *,
    owner_id: uuid.UUID | None,
) -> None:
    query = select(Reservation).where(Reservation.id == reservation_id).with_for_update()
    if owner_id is not None:
        query = query.where(Reservation.user_id == owner_id)
    reservation = await session.scalar(query)
    if reservation is None:
        raise ReservationNotFoundError("reservation not found")
    if reservation.status is ReservationStatus.CANCELLED:
        return
    status = effective_status(reservation)
    if status not in ACTIVE_HOLD_STATUSES:
        raise ReservationNotCancellableError(status)
    # Too late once the machine is dispensing it (ADR-018).
    if await session.scalar(
        select(DispenseCommand.id).where(
            DispenseCommand.reservation_id == reservation.id,
            DispenseCommand.status == DispenseCommandStatus.OPEN,
        )
    ):
        raise ReservationBeingDispensedError("the item is being dispensed")
    reservation.status = ReservationStatus.CANCELLED
    slot_id = reservation.slot_id
    await session.commit()
    await _release_quietly(redis, slot_id, reservation_id)
    await settle_reservation(session, provider, reservation_id)
    await events.publish_for_slot(session, redis, Topic.RESERVATION, slot_id, change="cancelled")
