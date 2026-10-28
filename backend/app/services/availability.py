"""What a customer can actually buy, per slot.

    available = max(physical quantity - active holds, 0)

An active hold is a reservation in `held` or `paid` whose `expires_at` is
still in the future. Holds stop counting the moment they expire, even before
the expiry worker (Phase 4) marks them `expired`. Slots in a machine that is
not `active`, or holding an inactive product, have nothing available.

This is the single definition of availability; every read path uses it. Phase 4
puts the authoritative hold check in Redis, and that must agree with this.
"""

from sqlalchemy import Subquery, and_, case, func, select

from app.models import Machine, MachineSlot, MachineStatus, Product, Reservation, ReservationStatus

ACTIVE_HOLD_STATUSES = (ReservationStatus.HELD, ReservationStatus.PAID)


def active_holds_by_slot() -> Subquery:
    return (
        select(Reservation.slot_id, func.sum(Reservation.quantity).label("held"))
        .where(
            Reservation.status.in_(ACTIVE_HOLD_STATUSES),
            Reservation.expires_at > func.now(),
        )
        .group_by(Reservation.slot_id)
        .subquery("active_holds")
    )


def slot_availability() -> Subquery:
    """One row per slot: slot_id, machine_id, product_id, available, low_stock."""
    holds = active_holds_by_slot()
    sellable = and_(Machine.status == MachineStatus.ACTIVE, Product.is_active.is_(True))
    available = case(
        (sellable, func.greatest(MachineSlot.quantity - func.coalesce(holds.c.held, 0), 0)),
        else_=0,
    )
    return (
        select(
            MachineSlot.id.label("slot_id"),
            MachineSlot.machine_id,
            MachineSlot.product_id,
            available.label("available"),
            (available <= MachineSlot.low_stock_threshold).label("low_stock"),
        )
        .join(Machine, Machine.id == MachineSlot.machine_id)
        .outerjoin(Product, Product.id == MachineSlot.product_id)
        .outerjoin(holds, holds.c.slot_id == MachineSlot.id)
        .subquery("slot_availability")
    )
