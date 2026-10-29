"""Builders for persisted test data. Each takes overrides and flushes (never commits)."""

import itertools
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Machine,
    MachineSlot,
    Order,
    PaymentStatus,
    Product,
    ProductCategory,
    Reservation,
    ReservationStatus,
    User,
    UserRole,
)
from app.schemas.common import GeoPoint
from app.services.geo import to_geography

_counter = itertools.count(1)

# Placeholder; real hashing arrives with authentication. Never a valid argon2 hash.
UNUSABLE_PASSWORD_HASH = "!unusable"


async def make_user(session: AsyncSession, **overrides: Any) -> User:
    n = next(_counter)
    user = User(
        email=overrides.pop("email", f"user{n}@example.com"),
        password_hash=overrides.pop("password_hash", UNUSABLE_PASSWORD_HASH),
        full_name=overrides.pop("full_name", f"Test User {n}"),
        role=overrides.pop("role", UserRole.CUSTOMER),
        **overrides,
    )
    session.add(user)
    await session.flush()
    return user


async def make_product(session: AsyncSession, **overrides: Any) -> Product:
    n = next(_counter)
    product = Product(
        sku=overrides.pop("sku", f"TEST-{n:04d}"),
        name=overrides.pop("name", f"Test product {n}"),
        category=overrides.pop("category", ProductCategory.HYGIENE),
        price_cents=overrides.pop("price_cents", 999),
        currency=overrides.pop("currency", "RON"),
        **overrides,
    )
    session.add(product)
    await session.flush()
    return product


async def make_machine(
    session: AsyncSession, *, lat: float = 44.4355, lng: float = 26.1025, **overrides: Any
) -> Machine:
    n = next(_counter)
    machine = Machine(
        serial_number=overrides.pop("serial_number", f"CP-TEST-{n:04d}"),
        name=overrides.pop("name", f"Test machine {n}"),
        address=overrides.pop("address", f"{n} Test Street"),
        location=to_geography(GeoPoint(lat=lat, lng=lng)),
        **overrides,
    )
    session.add(machine)
    await session.flush()
    return machine


async def make_slot(
    session: AsyncSession, *, machine: Machine, product: Product | None, **overrides: Any
) -> MachineSlot:
    n = next(_counter)
    slot = MachineSlot(
        machine_id=machine.id,
        product_id=product.id if product else None,
        slot_code=overrides.pop("slot_code", f"A{n % 100}"),
        capacity=overrides.pop("capacity", 10),
        **overrides,
    )
    session.add(slot)
    await session.flush()
    return slot


async def make_reservation(
    session: AsyncSession,
    *,
    user: User,
    slot: MachineSlot,
    quantity: int = 1,
    status: ReservationStatus = ReservationStatus.HELD,
    expires_in: timedelta = timedelta(minutes=15),
) -> Reservation:
    reservation = Reservation(
        user_id=user.id,
        slot_id=slot.id,
        quantity=quantity,
        unit_price_cents=999,
        status=status,
        expires_at=datetime.now(UTC) + expires_in,
    )
    session.add(reservation)
    await session.flush()
    return reservation


async def make_order(
    session: AsyncSession,
    *,
    reservation: Reservation,
    status: PaymentStatus = PaymentStatus.AUTHORIZED,
) -> Order:
    order = Order(
        reservation_id=reservation.id,
        user_id=reservation.user_id,
        amount_cents=reservation.unit_price_cents * reservation.quantity,
        currency="RON",
        payment_status=status,
        payment_reference=f"mock_{reservation.id.hex}",
    )
    session.add(order)
    await session.flush()
    return order
