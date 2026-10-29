"""Order history (read side)."""

import uuid

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MachineSlot, Order, Product, Reservation, User
from app.schemas.reservation import OrderRead
from app.services.reservations import effective_status


class OrderNotFoundError(Exception):
    pass


def _query() -> Select[tuple[Order, str, str, Reservation]]:
    return (
        select(Order, Product.sku, Product.name, Reservation)
        .join(Reservation, Reservation.id == Order.reservation_id)
        .join(MachineSlot, MachineSlot.id == Reservation.slot_id)
        .join(Product, Product.id == MachineSlot.product_id)
    )


def to_read(order: Order, sku: str, name: str, reservation: Reservation) -> OrderRead:
    return OrderRead(
        id=order.id,
        reservation_id=order.reservation_id,
        product_sku=sku,
        product_name=name,
        quantity=reservation.quantity,
        amount_cents=order.amount_cents,
        currency=order.currency,
        payment_status=order.payment_status,
        reservation_status=effective_status(reservation),
        created_at=order.created_at,
        updated_at=order.updated_at,
    )


async def list_orders(session: AsyncSession, user: User, *, limit: int = 50) -> list[OrderRead]:
    rows = await session.execute(
        _query()
        .where(Order.user_id == user.id)
        .order_by(Order.created_at.desc(), Order.id)
        .limit(limit)
    )
    return [to_read(*row) for row in rows]


async def get_order(session: AsyncSession, user: User, order_id: uuid.UUID) -> OrderRead:
    """A user's own order; someone else's is reported as not found."""
    row = (
        await session.execute(_query().where(Order.id == order_id, Order.user_id == user.id))
    ).one_or_none()
    if row is None:
        raise OrderNotFoundError(str(order_id))
    return to_read(*row)


async def order_for_reservation(session: AsyncSession, reservation_id: uuid.UUID) -> OrderRead:
    row = (await session.execute(_query().where(Order.reservation_id == reservation_id))).one()
    return to_read(*row)
