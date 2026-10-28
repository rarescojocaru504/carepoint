"""Idempotent demo-data seeding.

Each entity is matched on its natural key (email, SKU, serial number,
machine + slot code). Missing rows are created; existing rows are left exactly
as they are, so re-running never duplicates data, never resets live stock and
never overwrites operator edits or changed passwords.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import hash_password
from app.models import Machine, MachineSlot, Product, StockMovementReason, User, UserRole
from app.schemas.common import GeoPoint
from app.services import demo_data
from app.services.geo import to_geography
from app.services.inventory import record_stock_movement


@dataclass
class SeedReport:
    created: dict[str, int] = field(
        default_factory=lambda: dict.fromkeys(("users", "products", "machines", "slots"), 0)
    )
    existing: dict[str, int] = field(
        default_factory=lambda: dict.fromkeys(("users", "products", "machines", "slots"), 0)
    )

    def count(self, kind: str, *, created: bool) -> None:
        (self.created if created else self.existing)[kind] += 1


async def seed_demo_data(session: AsyncSession, passwords: Mapping[UserRole, str]) -> SeedReport:
    """Insert the demo dataset. Flushes but does not commit."""
    report = SeedReport()
    await _seed_users(session, passwords, report)
    products = await _seed_products(session, report)
    await _seed_machines(session, products, report)
    return report


async def _seed_users(
    session: AsyncSession, passwords: Mapping[UserRole, str], report: SeedReport
) -> None:
    for spec in demo_data.USERS:
        if await session.scalar(select(User.id).where(User.email == spec.email)):
            report.count("users", created=False)
            continue
        session.add(
            User(
                email=spec.email,
                full_name=spec.full_name,
                role=spec.role,
                password_hash=hash_password(passwords[spec.role]),
            )
        )
        report.count("users", created=True)
    await session.flush()


async def _seed_products(session: AsyncSession, report: SeedReport) -> dict[str, Product]:
    currency = get_settings().currency
    products: dict[str, Product] = {}
    for spec in demo_data.PRODUCTS:
        product = await session.scalar(select(Product).where(Product.sku == spec.sku))
        report.count("products", created=product is None)
        if product is None:
            product = Product(
                sku=spec.sku,
                name=spec.name,
                description=spec.description,
                category=spec.category,
                price_cents=spec.price_cents,
                currency=currency,
            )
            session.add(product)
        products[spec.sku] = product
    await session.flush()
    return products


async def _seed_machines(
    session: AsyncSession, products: Mapping[str, Product], report: SeedReport
) -> None:
    for spec in demo_data.MACHINES:
        machine = await session.scalar(
            select(Machine).where(Machine.serial_number == spec.serial_number)
        )
        report.count("machines", created=machine is None)
        if machine is None:
            machine = Machine(
                serial_number=spec.serial_number,
                name=spec.name,
                address=spec.address,
                location=to_geography(GeoPoint(lat=spec.lat, lng=spec.lng)),
                status=spec.status,
            )
            session.add(machine)
            await session.flush()

        existing_codes = set(
            await session.scalars(
                select(MachineSlot.slot_code).where(MachineSlot.machine_id == machine.id)
            )
        )
        for slot_spec in spec.slots:
            if slot_spec.slot_code in existing_codes:
                report.count("slots", created=False)
                continue
            slot = MachineSlot(
                machine_id=machine.id,
                product_id=products[slot_spec.sku].id if slot_spec.sku else None,
                slot_code=slot_spec.slot_code,
                capacity=slot_spec.capacity,
                low_stock_threshold=slot_spec.low_stock_threshold,
            )
            session.add(slot)
            await session.flush()
            # Stock enters through the ledger, so history is complete from day one.
            if slot_spec.initial_quantity:
                await record_stock_movement(
                    session,
                    slot_id=slot.id,
                    delta=slot_spec.initial_quantity,
                    reason=StockMovementReason.INITIAL,
                    note="demo seed",
                )
            report.count("slots", created=True)
