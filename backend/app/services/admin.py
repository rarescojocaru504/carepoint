"""What operators see and do (ADR-021): machines and their slots, stock
changes recorded by people, reservations across all customers, the catalogue."""

import logging
import uuid
from collections import defaultdict
from datetime import UTC, datetime

from geoalchemy2 import Geometry
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import ColumnElement, cast, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import (
    Machine,
    MachineEvent,
    MachineSlot,
    MachineStatus,
    Order,
    Product,
    Reservation,
    StockMovement,
    StockMovementReason,
    User,
)
from app.schemas.admin import (
    AdminMachine,
    AdminMachineDetail,
    AdminReservation,
    AdminSlot,
    MachineEventRead,
    OperatorStockChange,
    ProductBrief,
    ReservationFilter,
    StockMovementResult,
    StockMovementWithActor,
)
from app.schemas.discovery import Location
from app.schemas.machine_protocol import AdjustStock
from app.schemas.product import ProductCreate, ProductUpdate
from app.services import hold_store
from app.services.availability import ACTIVE_HOLD_STATUSES, active_holds_by_slot
from app.services.inventory import record_stock_movement
from app.services.machine_gateway import send_command
from app.services.machine_status import is_online
from app.services.reservations import effective_status

logger = logging.getLogger(__name__)


class AdminError(Exception):
    pass


class NotFoundError(AdminError):
    pass


class MachineOfflineError(AdminError):
    """The machine must be connected: it keeps its own count of the units."""


class ConflictError(AdminError):
    pass


# --- machines -------------------------------------------------------------------


def _location_columns() -> tuple[ColumnElement[float], ColumnElement[float]]:
    geometry = cast(Machine.location, Geometry)
    return func.ST_Y(geometry), func.ST_X(geometry)


async def _slots_by_machine(
    session: AsyncSession, machine_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[AdminSlot]]:
    holds = active_holds_by_slot()
    rows = await session.execute(
        select(MachineSlot, Product, func.coalesce(holds.c.held, 0))
        .outerjoin(Product, Product.id == MachineSlot.product_id)
        .outerjoin(holds, holds.c.slot_id == MachineSlot.id)
        .where(MachineSlot.machine_id.in_(machine_ids))
        .order_by(MachineSlot.slot_code)
    )
    slots: dict[uuid.UUID, list[AdminSlot]] = defaultdict(list)
    for slot, product, held in rows:
        assigned = product is not None
        slots[slot.machine_id].append(
            AdminSlot(
                id=slot.id,
                slot_code=slot.slot_code,
                product=ProductBrief(id=product.id, sku=product.sku, name=product.name)
                if product is not None
                else None,
                quantity=slot.quantity,
                capacity=slot.capacity,
                low_stock_threshold=slot.low_stock_threshold,
                held=int(held),
                available=max(slot.quantity - int(held), 0),
                low_stock=assigned and 0 < slot.quantity <= slot.low_stock_threshold,
                sold_out=assigned and slot.quantity == 0,
            )
        )
    return slots


async def _active_reservations(
    session: AsyncSession, machine_ids: list[uuid.UUID]
) -> dict[uuid.UUID, int]:
    rows = await session.execute(
        select(MachineSlot.machine_id, func.count(Reservation.id))
        .join(Reservation, Reservation.slot_id == MachineSlot.id)
        .where(
            MachineSlot.machine_id.in_(machine_ids),
            Reservation.status.in_(ACTIVE_HOLD_STATUSES),
            Reservation.expires_at > func.now(),
        )
        .group_by(MachineSlot.machine_id)
    )
    return {machine_id: int(count) for machine_id, count in rows}


def _summary(
    machine: Machine, lat: float, lng: float, slots: list[AdminSlot], active: int
) -> AdminMachine:
    window = get_settings().machine_online_window_seconds
    return AdminMachine(
        id=machine.id,
        serial_number=machine.serial_number,
        name=machine.name,
        address=machine.address,
        location=Location(lat=lat, lng=lng),
        status=machine.status,
        online=is_online(machine, window),
        last_seen_at=machine.last_seen_at,
        firmware_version=machine.firmware_version,
        door_open=machine.door_open,
        temperature_c=float(machine.temperature_c) if machine.temperature_c is not None else None,
        telemetry_at=machine.telemetry_at,
        units=sum(s.quantity for s in slots),
        low_stock_slots=sum(s.low_stock for s in slots),
        sold_out_slots=sum(s.sold_out for s in slots),
        active_reservations=active,
    )


async def list_machines(session: AsyncSession) -> list[AdminMachine]:
    lat, lng = _location_columns()
    rows = (
        await session.execute(
            select(Machine, lat, lng)
            .where(Machine.status != MachineStatus.DECOMMISSIONED)
            .order_by(Machine.serial_number)
        )
    ).all()
    ids = [machine.id for machine, _, _ in rows]
    slots = await _slots_by_machine(session, ids)
    active = await _active_reservations(session, ids)
    return [
        _summary(machine, la, ln, slots[machine.id], active.get(machine.id, 0))
        for machine, la, ln in rows
    ]


async def get_machine(session: AsyncSession, machine_id: uuid.UUID) -> AdminMachineDetail:
    lat, lng = _location_columns()
    row = (await session.execute(select(Machine, lat, lng).where(Machine.id == machine_id))).first()
    if row is None:
        raise NotFoundError("machine not found")
    machine, la, ln = row
    slots = (await _slots_by_machine(session, [machine.id]))[machine.id]
    active = (await _active_reservations(session, [machine.id])).get(machine.id, 0)
    events = await session.scalars(
        select(MachineEvent)
        .where(MachineEvent.machine_id == machine.id)
        .order_by(MachineEvent.id.desc())
        .limit(20)
    )
    return AdminMachineDetail(
        **_summary(machine, la, ln, slots, active).model_dump(),
        slots=slots,
        recent_events=[MachineEventRead.model_validate(event) for event in events],
    )


async def set_machine_status(
    session: AsyncSession, machine_id: uuid.UUID, status: MachineStatus
) -> AdminMachineDetail:
    machine = await session.get(Machine, machine_id, with_for_update=True)
    if machine is None:
        raise NotFoundError("machine not found")
    machine.status = status
    await session.commit()
    return await get_machine(session, machine_id)


# --- stock ----------------------------------------------------------------------


async def record_operator_change(
    session: AsyncSession, redis: Redis, actor: User, change: OperatorStockChange
) -> StockMovementResult:
    """Record a restock or adjustment and tell the machine (adjust_stock).

    Refused while the machine is offline: it keeps its own count, and a change
    it never heard of would be undone by its next stock report (ADR-021).
    """
    slot = await session.get(MachineSlot, change.slot_id)
    if slot is None:
        raise NotFoundError("slot not found")
    machine = await session.get(Machine, slot.machine_id)
    if machine is None:  # foreign keys forbid it
        raise NotFoundError("machine not found")
    if not is_online(machine, get_settings().machine_online_window_seconds):
        raise MachineOfflineError(
            f"{machine.serial_number} is offline; record stock changes while it is connected"
        )
    movement = await record_stock_movement(
        session,
        slot_id=slot.id,
        delta=change.delta,
        reason=StockMovementReason(change.reason),
        actor_user_id=actor.id,
        note=change.note,
    )
    machine_id, slot_code = machine.id, slot.slot_code
    await session.commit()

    try:
        await hold_store.apply_stock_delta(redis, change.slot_id, change.delta)
    except RedisError:
        logger.exception("Could not mirror stock change on slot %s", change.slot_id)
    notified = False
    try:
        notified = await send_command(
            redis,
            machine_id,
            AdjustStock(
                command_id=uuid.uuid4(), slot=slot_code, delta=change.delta, reason=change.reason
            ),
        )
    except RedisError:
        logger.exception("Could not send adjust_stock to %s", machine_id)
    if not notified:
        logger.warning("Machine %s did not receive adjust_stock for %s", machine_id, slot_code)
    return StockMovementResult(
        **StockMovementWithActor.model_validate(
            {**_movement_fields(movement), "actor_email": actor.email}
        ).model_dump(),
        machine_notified=notified,
    )


def _movement_fields(movement: StockMovement) -> dict[str, object]:
    return {
        column: getattr(movement, column)
        for column in (
            "id", "slot_id", "delta", "quantity_after", "reason", "actor_user_id",
            "reservation_id", "note", "created_at",
        )
    }  # fmt: skip


async def stock_history(
    session: AsyncSession, slot_id: uuid.UUID, *, limit: int
) -> list[StockMovementWithActor]:
    if await session.get(MachineSlot, slot_id) is None:
        raise NotFoundError("slot not found")
    rows = await session.execute(
        select(StockMovement, User.email)
        .outerjoin(User, User.id == StockMovement.actor_user_id)
        .where(StockMovement.slot_id == slot_id)
        .order_by(StockMovement.id.desc())
        .limit(limit)
    )
    return [
        StockMovementWithActor.model_validate({**_movement_fields(m), "actor_email": email})
        for m, email in rows
    ]


# --- reservations ---------------------------------------------------------------


async def list_reservations(
    session: AsyncSession,
    *,
    status: ReservationFilter | None,
    machine_id: uuid.UUID | None,
    limit: int,
) -> list[AdminReservation]:
    query = (
        select(Reservation, User.email, MachineSlot, Machine.serial_number, Product, Order)
        .join(User, User.id == Reservation.user_id)
        .join(MachineSlot, MachineSlot.id == Reservation.slot_id)
        .join(Machine, Machine.id == MachineSlot.machine_id)
        .join(Product, Product.id == MachineSlot.product_id)
        .outerjoin(Order, Order.reservation_id == Reservation.id)
        .order_by(Reservation.created_at.desc(), Reservation.id)
        .limit(limit)
    )
    if machine_id is not None:
        query = query.where(MachineSlot.machine_id == machine_id)
    if status == "active":
        query = query.where(
            Reservation.status.in_(ACTIVE_HOLD_STATUSES), Reservation.expires_at > func.now()
        )
    elif status == "expired":
        query = query.where(
            (Reservation.status == "expired")
            | (
                Reservation.status.in_(ACTIVE_HOLD_STATUSES)
                & (Reservation.expires_at <= func.now())
            )
        )
    elif status in ("held", "paid"):
        query = query.where(Reservation.status == status, Reservation.expires_at > func.now())
    elif status is not None:
        query = query.where(Reservation.status == status)

    now = datetime.now(UTC)
    return [
        AdminReservation(
            id=r.id,
            status=effective_status(r, now),
            user_email=email,
            machine_id=slot.machine_id,
            machine_serial=serial,
            slot_code=slot.slot_code,
            product_sku=product.sku,
            product_name=product.name,
            quantity=r.quantity,
            total_cents=r.quantity * r.unit_price_cents,
            payment_status=order.payment_status if order is not None else None,
            created_at=r.created_at,
            expires_at=r.expires_at,
        )
        for r, email, slot, serial, product, order in await session.execute(query)
    ]


# --- products -------------------------------------------------------------------


async def list_products(session: AsyncSession) -> list[Product]:
    return list(await session.scalars(select(Product).order_by(Product.sku)))


async def create_product(session: AsyncSession, data: ProductCreate) -> Product:
    product = Product(**data.model_dump(mode="json"))
    try:
        # A savepoint, so a duplicate SKU undoes only this insert.
        async with session.begin_nested():
            session.add(product)
            await session.flush()
    except IntegrityError:
        raise ConflictError(f"SKU {data.sku} already exists") from None
    await session.commit()
    return product


async def update_product(
    session: AsyncSession, product_id: uuid.UUID, data: ProductUpdate
) -> Product:
    product = await session.get(Product, product_id, with_for_update=True)
    if product is None:
        raise NotFoundError("product not found")
    for field, value in data.model_dump(mode="json", exclude_unset=True).items():
        setattr(product, field, value)
    await session.commit()
    await session.refresh(product)
    return product


async def delete_product(session: AsyncSession, product_id: uuid.UUID) -> None:
    """Delete a product no slot uses; one in use must be deactivated instead,
    so the history of what was sold stays intact."""
    product = await session.get(Product, product_id, with_for_update=True)
    if product is None:
        raise NotFoundError("product not found")
    used = await session.scalar(
        select(func.count()).select_from(MachineSlot).where(MachineSlot.product_id == product_id)
    )
    if used:
        raise ConflictError(f"{product.sku} is assigned to {used} slot(s); deactivate it instead")
    await session.delete(product)
    await session.commit()
