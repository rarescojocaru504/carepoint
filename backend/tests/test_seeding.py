import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import verify_password
from app.models import (
    Machine,
    MachineSlot,
    MachineStatus,
    Product,
    StockMovement,
    StockMovementReason,
    User,
    UserRole,
)
from app.services import demo_data
from app.services.inventory import record_stock_movement
from app.services.seeding import seed_demo_data

pytestmark = pytest.mark.integration

PASSWORDS = {
    UserRole.ADMIN: "admin-password-for-tests",
    UserRole.OPERATOR: "operator-password-for-tests",
    UserRole.CUSTOMER: "customer-password-for-tests",
}

# Generous box around Bucharest; catches swapped lat/lng.
BUCHAREST_LAT = (44.33, 44.55)
BUCHAREST_LNG = (25.95, 26.25)


async def _counts(session: AsyncSession) -> dict[str, int]:
    counts: dict[str, int] = {}
    for name, model in {
        "users": User,
        "products": Product,
        "machines": Machine,
        "slots": MachineSlot,
        "movements": StockMovement,
    }.items():
        counts[name] = await session.scalar(select(func.count()).select_from(model)) or 0
    counts["units"] = await session.scalar(select(func.sum(MachineSlot.quantity))) or 0
    return counts


async def test_seed_creates_expected_dataset(db_session: AsyncSession) -> None:
    report = await seed_demo_data(db_session, PASSWORDS)

    expected_slots = sum(len(m.slots) for m in demo_data.MACHINES)
    assert report.created == {
        "users": len(demo_data.USERS),
        "products": len(demo_data.PRODUCTS),
        "machines": len(demo_data.MACHINES),
        "slots": expected_slots,
    }
    counts = await _counts(db_session)
    assert counts["slots"] == expected_slots
    stocked = [s for m in demo_data.MACHINES for s in m.slots if s.initial_quantity]
    assert counts["movements"] == len(stocked)
    assert counts["units"] == sum(s.initial_quantity for s in stocked)


async def test_seed_is_idempotent(db_session: AsyncSession) -> None:
    await seed_demo_data(db_session, PASSWORDS)
    before = await _counts(db_session)

    report = await seed_demo_data(db_session, PASSWORDS)

    assert set(report.created.values()) == {0}
    assert await _counts(db_session) == before


async def test_reseed_preserves_live_changes(db_session: AsyncSession) -> None:
    """A re-run must not reset stock, prices or passwords changed since the first run."""
    await seed_demo_data(db_session, PASSWORDS)
    slot = await db_session.scalar(select(MachineSlot).where(MachineSlot.quantity > 0).limit(1))
    assert slot is not None
    slot_id, expected_quantity = slot.id, slot.quantity - 1
    await record_stock_movement(
        db_session, slot_id=slot_id, delta=-1, reason=StockMovementReason.DISPENSE
    )
    product = await db_session.scalar(select(Product).where(Product.sku == "CONDOM-3"))
    assert product is not None
    product.price_cents = 2199
    await db_session.flush()

    await seed_demo_data(db_session, {role: "a-different-password" for role in UserRole})

    db_session.expire_all()
    refreshed = await db_session.get(MachineSlot, slot_id)
    assert refreshed is not None
    assert refreshed.quantity == expected_quantity
    assert (
        await db_session.scalar(select(Product.price_cents).where(Product.sku == "CONDOM-3"))
        == 2199
    )
    admin_hash = await db_session.scalar(
        select(User.password_hash).where(User.email == "admin@carepoint.demo")
    )
    assert admin_hash is not None
    assert verify_password(admin_hash, PASSWORDS[UserRole.ADMIN])


async def test_seed_users_roles_and_hashed_passwords(db_session: AsyncSession) -> None:
    await seed_demo_data(db_session, PASSWORDS)
    users = {u.email: u for u in await db_session.scalars(select(User))}
    for spec in demo_data.USERS:
        user = users[spec.email]
        assert user.role is spec.role
        assert user.password_hash.startswith("$argon2id$")
        assert verify_password(user.password_hash, PASSWORDS[spec.role])
        assert not verify_password(user.password_hash, "wrong-password")


async def test_seed_machines_are_located_in_bucharest(db_session: AsyncSession) -> None:
    await seed_demo_data(db_session, PASSWORDS)
    rows = (
        await db_session.execute(
            select(
                Machine.serial_number,
                Machine.status,
                func.ST_Y(func.ST_GeomFromWKB(func.ST_AsBinary(Machine.location))),
                func.ST_X(func.ST_GeomFromWKB(func.ST_AsBinary(Machine.location))),
            )
        )
    ).all()
    assert len(rows) == len(demo_data.MACHINES)
    for _serial, _status, lat, lng in rows:
        assert BUCHAREST_LAT[0] < lat < BUCHAREST_LAT[1]
        assert BUCHAREST_LNG[0] < lng < BUCHAREST_LNG[1]
    # The demo includes a machine under maintenance, for dashboard/app states.
    assert MachineStatus.MAINTENANCE in {row.status for row in rows}


async def test_seed_ledger_matches_stock(db_session: AsyncSession) -> None:
    await seed_demo_data(db_session, PASSWORDS)
    mismatches = (
        await db_session.execute(
            select(MachineSlot.slot_code, MachineSlot.quantity, func.sum(StockMovement.delta))
            .outerjoin(StockMovement, StockMovement.slot_id == MachineSlot.id)
            .group_by(MachineSlot.id)
            .having(MachineSlot.quantity != func.coalesce(func.sum(StockMovement.delta), 0))
        )
    ).all()
    assert mismatches == []
    reasons = set(await db_session.scalars(select(StockMovement.reason).distinct()))
    assert reasons == {StockMovementReason.INITIAL}


def test_demo_data_is_internally_consistent() -> None:
    skus = {p.sku for p in demo_data.PRODUCTS}
    assert len(skus) == len(demo_data.PRODUCTS)
    serials = [m.serial_number for m in demo_data.MACHINES]
    assert len(set(serials)) == len(serials)
    for machine in demo_data.MACHINES:
        codes = [s.slot_code for s in machine.slots]
        assert len(set(codes)) == len(codes), machine.serial_number
        for slot in machine.slots:
            assert slot.sku is None or slot.sku in skus
            assert 0 <= slot.initial_quantity <= slot.capacity
            assert slot.sku is not None or slot.initial_quantity == 0
