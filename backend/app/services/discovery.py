"""Read-side queries for the map, machine pages and product search."""

import uuid
from datetime import UTC, datetime, timedelta

from geoalchemy2 import Geography, Geometry
from sqlalchemy import ColumnElement, Select, and_, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Machine, MachineSlot, MachineStatus, Product, ProductCategory
from app.schemas.common import GeoPoint
from app.schemas.discovery import (
    Location,
    MachineDetail,
    MachineSummary,
    ProductLocation,
    ProductNearby,
    SlotAvailability,
)
from app.schemas.product import ProductRead
from app.services.availability import slot_availability


class MachineNotFoundError(Exception):
    pass


class ProductNotFoundError(Exception):
    pass


def _point(point: GeoPoint) -> ColumnElement[bytes]:
    # Bound float parameters, never string-formatted into SQL.
    return cast(func.ST_SetSRID(func.ST_MakePoint(point.lng, point.lat), 4326), Geography)


def _lat() -> ColumnElement[float]:
    return func.ST_Y(cast(Machine.location, Geometry))


def _lng() -> ColumnElement[float]:
    return func.ST_X(cast(Machine.location, Geometry))


def _is_online(last_seen_at: datetime | None, window_seconds: int) -> bool:
    return last_seen_at is not None and last_seen_at >= datetime.now(UTC) - timedelta(
        seconds=window_seconds
    )


def _visible() -> ColumnElement[bool]:
    """Decommissioned machines never appear to customers."""
    return Machine.status != MachineStatus.DECOMMISSIONED


async def find_nearby_machines(
    session: AsyncSession,
    point: GeoPoint,
    *,
    radius_m: int,
    limit: int,
    online_window_seconds: int,
) -> list[MachineSummary]:
    origin = _point(point)
    slots = slot_availability()
    per_machine = (
        select(
            slots.c.machine_id,
            func.count(func.distinct(slots.c.product_id))
            .filter(slots.c.available > 0)
            .label("products_available"),
            func.coalesce(func.sum(slots.c.available), 0).label("units_available"),
        )
        .group_by(slots.c.machine_id)
        .subquery("per_machine")
    )
    distance = func.ST_Distance(Machine.location, origin)
    query: Select[tuple[Machine, float, float, float, int | None, int | None]] = (
        select(
            Machine,
            distance.label("distance_m"),
            _lat(),
            _lng(),
            per_machine.c.products_available,
            per_machine.c.units_available,
        )
        .outerjoin(per_machine, per_machine.c.machine_id == Machine.id)
        # ST_DWithin uses the GiST index; ordering by distance after filtering.
        .where(_visible(), func.ST_DWithin(Machine.location, origin, radius_m))
        .order_by(distance, Machine.id)
        .limit(limit)
    )
    rows = (await session.execute(query)).all()
    return [
        MachineSummary(
            id=machine.id,
            name=machine.name,
            address=machine.address,
            location=Location(lat=lat, lng=lng),
            status=machine.status,
            online=_is_online(machine.last_seen_at, online_window_seconds),
            distance_m=round(dist, 1),
            products_available=products or 0,
            units_available=int(units or 0),
        )
        for machine, dist, lat, lng, products, units in rows
    ]


async def get_machine_detail(
    session: AsyncSession, machine_id: uuid.UUID, *, online_window_seconds: int
) -> MachineDetail:
    row = (
        await session.execute(
            select(Machine, _lat(), _lng()).where(Machine.id == machine_id, _visible())
        )
    ).one_or_none()
    if row is None:
        raise MachineNotFoundError(str(machine_id))
    machine, lat, lng = row

    slots = slot_availability()
    slot_rows = (
        await session.execute(
            select(
                MachineSlot.id, MachineSlot.slot_code, Product, slots.c.available, slots.c.low_stock
            )
            .join(slots, slots.c.slot_id == MachineSlot.id)
            .join(Product, Product.id == MachineSlot.product_id)  # empty slots are omitted
            .where(MachineSlot.machine_id == machine_id, Product.is_active.is_(True))
            .order_by(MachineSlot.slot_code)
        )
    ).all()
    return MachineDetail(
        id=machine.id,
        name=machine.name,
        address=machine.address,
        location=Location(lat=lat, lng=lng),
        status=machine.status,
        online=_is_online(machine.last_seen_at, online_window_seconds),
        last_seen_at=machine.last_seen_at,
        slots=[
            SlotAvailability(
                slot_id=slot_id,
                slot_code=code,
                product=ProductRead.model_validate(product),
                available=available,
                low_stock=low_stock,
            )
            for slot_id, code, product, available, low_stock in slot_rows
        ],
    )


async def list_products(
    session: AsyncSession, category: ProductCategory | None = None
) -> list[ProductRead]:
    query = (
        select(Product).where(Product.is_active.is_(True)).order_by(Product.category, Product.name)
    )
    if category is not None:
        query = query.where(Product.category == category)
    return [ProductRead.model_validate(p) for p in await session.scalars(query)]


async def find_product_nearby(
    session: AsyncSession,
    sku: str,
    point: GeoPoint,
    *,
    radius_m: int,
    limit: int,
    online_window_seconds: int,
) -> ProductNearby:
    product = await session.scalar(
        select(Product).where(Product.sku == sku, Product.is_active.is_(True))
    )
    if product is None:
        raise ProductNotFoundError(sku)

    origin = _point(point)
    slots = slot_availability()
    # Per machine: total available of this SKU, and the slot with the most units
    # (DISTINCT ON keeps the first row per machine in the ORDER BY).
    best_slot = (
        select(
            slots.c.machine_id,
            slots.c.slot_id,
            func.sum(slots.c.available).over(partition_by=slots.c.machine_id).label("total"),
        )
        .where(slots.c.product_id == product.id, slots.c.available > 0)
        .distinct(slots.c.machine_id)
        .order_by(slots.c.machine_id, slots.c.available.desc(), slots.c.slot_id)
        .subquery("best_slot")
    )
    distance = func.ST_Distance(Machine.location, origin)
    rows = (
        await session.execute(
            select(Machine, distance, _lat(), _lng(), best_slot.c.slot_id, best_slot.c.total)
            .join(best_slot, best_slot.c.machine_id == Machine.id)
            .where(and_(_visible(), func.ST_DWithin(Machine.location, origin, radius_m)))
            .order_by(distance, Machine.id)
            .limit(limit)
        )
    ).all()
    return ProductNearby(
        product=ProductRead.model_validate(product),
        locations=[
            ProductLocation(
                machine_id=machine.id,
                machine_name=machine.name,
                address=machine.address,
                location=Location(lat=lat, lng=lng),
                online=_is_online(machine.last_seen_at, online_window_seconds),
                distance_m=round(dist, 1),
                available=int(total),
                slot_id=slot_id,
            )
            for machine, dist, lat, lng, slot_id, total in rows
        ],
    )
