import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MachineSlot, StockMovement, StockMovementReason


class InventoryError(Exception):
    """Base class for rejected stock changes."""


class SlotNotFoundError(InventoryError):
    pass


class EmptySlotError(InventoryError):
    """The slot has no product assigned, so it cannot hold stock."""


class InsufficientStockError(InventoryError):
    pass


class CapacityExceededError(InventoryError):
    pass


async def record_stock_movement(
    session: AsyncSession,
    *,
    slot_id: uuid.UUID,
    delta: int,
    reason: StockMovementReason,
    actor_user_id: uuid.UUID | None = None,
    reservation_id: uuid.UUID | None = None,
    note: str | None = None,
) -> StockMovement:
    """Change a slot's physical quantity and append the matching ledger row.

    This is the only sanctioned way to change `MachineSlot.quantity`. The slot
    row is locked (`SELECT ... FOR UPDATE`) so concurrent movements on the same
    slot serialise, and quantity + ledger are written in the caller's
    transaction (this function flushes but never commits).
    """
    if delta == 0:
        raise ValueError("delta must be non-zero")

    slot = await session.scalar(
        select(MachineSlot)
        .where(MachineSlot.id == slot_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if slot is None:
        raise SlotNotFoundError(str(slot_id))
    if slot.product_id is None:
        raise EmptySlotError(slot.slot_code)

    new_quantity = slot.quantity + delta
    if new_quantity < 0:
        raise InsufficientStockError(f"slot {slot.slot_code} has {slot.quantity}, delta {delta}")
    if new_quantity > slot.capacity:
        raise CapacityExceededError(
            f"slot {slot.slot_code} capacity {slot.capacity}, would hold {new_quantity}"
        )

    slot.quantity = new_quantity
    movement = StockMovement(
        slot_id=slot.id,
        delta=delta,
        quantity_after=new_quantity,
        reason=reason,
        actor_user_id=actor_user_id,
        reservation_id=reservation_id,
        note=note,
    )
    session.add(movement)
    await session.flush()
    return movement
