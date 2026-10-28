"""Database-level guarantees: constraints, the append-only ledger, inventory service."""

import pytest
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Machine, MachineSlot, StockMovement, StockMovementReason, UserRole
from app.services.inventory import (
    CapacityExceededError,
    EmptySlotError,
    InsufficientStockError,
    SlotNotFoundError,
    record_stock_movement,
)
from tests.factories import make_machine, make_product, make_slot, make_user

pytestmark = pytest.mark.integration


async def _expect_db_error(session: AsyncSession, stmt: str) -> None:
    """Run raw SQL in a savepoint and assert the database rejects it."""
    with pytest.raises((IntegrityError, DBAPIError)):
        async with session.begin_nested():
            await session.execute(text(stmt))


async def _expect_db_ok(session: AsyncSession, stmt: str) -> None:
    """Positive control: the same SQL shape with valid values must succeed, proving the
    failing variants are rejected by the constraint (not by a typo in the SQL)."""
    async with session.begin_nested():
        await session.execute(text(stmt))


# --- users -----------------------------------------------------------------------


async def test_user_defaults_to_customer(db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    await db_session.refresh(user)
    assert user.role is UserRole.CUSTOMER
    assert user.is_active is True


async def test_user_email_must_be_lowercase(db_session: AsyncSession) -> None:
    await _expect_db_ok(
        db_session,
        "INSERT INTO users (email, password_hash, full_name) VALUES ('a@b.com', 'x', 'n')",
    )
    await _expect_db_error(
        db_session,
        "INSERT INTO users (email, password_hash, full_name) VALUES ('A@B.com', 'x', 'n')",
    )


async def test_user_email_unique(db_session: AsyncSession) -> None:
    await make_user(db_session, email="dup@example.com")
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await make_user(db_session, email="dup@example.com")


async def test_user_role_is_constrained(db_session: AsyncSession) -> None:
    await _expect_db_ok(
        db_session,
        "INSERT INTO users (email, password_hash, full_name, role) "
        "VALUES ('ok@b.com', 'x', 'n', 'operator')",
    )
    await _expect_db_error(
        db_session,
        "INSERT INTO users (email, password_hash, full_name, role) "
        "VALUES ('r@b.com', 'x', 'n', 'superuser')",
    )


# --- products --------------------------------------------------------------------


def _product_insert(**values: str) -> str:
    v = {"sku": "'OK-SKU'", "price_cents": "100", "currency": "'RON'", "category": "'hygiene'"}
    v |= values
    return (
        "INSERT INTO products (sku, name, category, price_cents, currency) "
        f"VALUES ({v['sku']}, 'n', {v['category']}, {v['price_cents']}, {v['currency']})"
    )


async def test_product_insert_control(db_session: AsyncSession) -> None:
    await _expect_db_ok(db_session, _product_insert())


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("price_cents", "0"),
        ("price_cents", "-5"),
        ("currency", "'ron'"),
        ("sku", "'bad sku'"),
        ("category", "'weapons'"),
    ],
)
async def test_product_constraints(db_session: AsyncSession, column: str, value: str) -> None:
    await _expect_db_error(db_session, _product_insert(**{column: value}))


# --- machines & slots ------------------------------------------------------------


async def test_machine_location_round_trips(db_session: AsyncSession) -> None:
    machine = await make_machine(db_session, lat=44.4355, lng=26.1025)
    lat, lng = (
        await db_session.execute(
            select(
                func.ST_Y(func.ST_GeomFromWKB(func.ST_AsBinary(Machine.location))),
                func.ST_X(func.ST_GeomFromWKB(func.ST_AsBinary(Machine.location))),
            ).where(Machine.id == machine.id)
        )
    ).one()
    assert lat == pytest.approx(44.4355)
    assert lng == pytest.approx(26.1025)


async def test_machine_public_key_must_be_32_bytes(db_session: AsyncSession) -> None:
    machine = await make_machine(db_session)
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                update(Machine).where(Machine.id == machine.id).values(auth_public_key=b"short")
            )
    async with db_session.begin_nested():
        await db_session.execute(
            update(Machine).where(Machine.id == machine.id).values(auth_public_key=b"k" * 32)
        )


@pytest.mark.parametrize(
    ("quantity", "capacity", "with_product"),
    [(11, 10, True), (-1, 10, True), (1, 0, True), (1, 10, False)],
    ids=["over-capacity", "negative", "zero-capacity", "stock-in-empty-slot"],
)
async def test_slot_constraints(
    db_session: AsyncSession, quantity: int, capacity: int, with_product: bool
) -> None:
    machine = await make_machine(db_session)
    product = await make_product(db_session) if with_product else None
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await make_slot(
                db_session, machine=machine, product=product, quantity=quantity, capacity=capacity
            )


async def test_slot_code_unique_per_machine(db_session: AsyncSession) -> None:
    machine = await make_machine(db_session)
    other = await make_machine(db_session)
    product = await make_product(db_session)
    await make_slot(db_session, machine=machine, product=product, slot_code="A1")
    await make_slot(db_session, machine=other, product=product, slot_code="A1")  # fine
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await make_slot(db_session, machine=machine, product=product, slot_code="A1")


async def test_machine_with_slots_cannot_be_deleted(db_session: AsyncSession) -> None:
    machine = await make_machine(db_session)
    await make_slot(db_session, machine=machine, product=None)
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(delete(Machine).where(Machine.id == machine.id))


# --- inventory service & ledger --------------------------------------------------


async def test_record_stock_movement_updates_quantity_and_ledger(
    db_session: AsyncSession,
) -> None:
    machine = await make_machine(db_session)
    product = await make_product(db_session)
    slot = await make_slot(db_session, machine=machine, product=product, capacity=10)
    operator = await make_user(db_session, role=UserRole.OPERATOR)

    await record_stock_movement(
        db_session, slot_id=slot.id, delta=8, reason=StockMovementReason.RESTOCK,
        actor_user_id=operator.id, note="weekly restock",
    )  # fmt: skip
    movement = await record_stock_movement(
        db_session, slot_id=slot.id, delta=-3, reason=StockMovementReason.DISPENSE
    )

    await db_session.refresh(slot)
    assert slot.quantity == 5
    assert movement.quantity_after == 5
    ledger = (
        await db_session.scalars(
            select(StockMovement).where(StockMovement.slot_id == slot.id).order_by(StockMovement.id)
        )
    ).all()
    assert [(m.delta, m.quantity_after, m.reason) for m in ledger] == [
        (8, 8, StockMovementReason.RESTOCK),
        (-3, 5, StockMovementReason.DISPENSE),
    ]
    assert ledger[0].actor_user_id == operator.id


async def test_ledger_sum_matches_quantity(db_session: AsyncSession) -> None:
    machine = await make_machine(db_session)
    product = await make_product(db_session)
    slot = await make_slot(db_session, machine=machine, product=product, capacity=20)
    for delta in (10, -2, 5, -7, 3):
        await record_stock_movement(
            db_session, slot_id=slot.id, delta=delta, reason=StockMovementReason.ADJUSTMENT
        )
    total = await db_session.scalar(
        select(func.sum(StockMovement.delta)).where(StockMovement.slot_id == slot.id)
    )
    await db_session.refresh(slot)
    assert total == slot.quantity == 9


@pytest.mark.parametrize(
    ("start", "delta", "error"),
    [(2, -3, InsufficientStockError), (9, 2, CapacityExceededError)],
)
async def test_record_stock_movement_rejects_out_of_bounds(
    db_session: AsyncSession, start: int, delta: int, error: type[Exception]
) -> None:
    machine = await make_machine(db_session)
    product = await make_product(db_session)
    slot = await make_slot(db_session, machine=machine, product=product, capacity=10)
    await record_stock_movement(
        db_session, slot_id=slot.id, delta=start, reason=StockMovementReason.INITIAL
    )
    with pytest.raises(error):
        await record_stock_movement(
            db_session, slot_id=slot.id, delta=delta, reason=StockMovementReason.ADJUSTMENT
        )
    await db_session.refresh(slot)
    assert slot.quantity == start


async def test_record_stock_movement_rejects_empty_slot_and_unknown_slot(
    db_session: AsyncSession,
) -> None:
    machine = await make_machine(db_session)
    empty = await make_slot(db_session, machine=machine, product=None)
    with pytest.raises(EmptySlotError):
        await record_stock_movement(
            db_session, slot_id=empty.id, delta=1, reason=StockMovementReason.RESTOCK
        )
    with pytest.raises(SlotNotFoundError):
        await record_stock_movement(
            db_session, slot_id=machine.id, delta=1, reason=StockMovementReason.RESTOCK
        )
    with pytest.raises(ValueError, match="non-zero"):
        await record_stock_movement(
            db_session, slot_id=empty.id, delta=0, reason=StockMovementReason.RESTOCK
        )


@pytest.mark.parametrize(
    "stmt",
    [
        "UPDATE stock_movements SET delta = 99",
        "DELETE FROM stock_movements",
        "TRUNCATE stock_movements CASCADE",
    ],
    ids=["update", "delete", "truncate"],
)
async def test_stock_movements_are_append_only(db_session: AsyncSession, stmt: str) -> None:
    machine = await make_machine(db_session)
    product = await make_product(db_session)
    slot = await make_slot(db_session, machine=machine, product=product)
    await record_stock_movement(
        db_session, slot_id=slot.id, delta=1, reason=StockMovementReason.INITIAL
    )
    with pytest.raises(DBAPIError, match="append-only"):
        async with db_session.begin_nested():
            await db_session.execute(text(stmt))


async def test_slot_relationships_load(db_session: AsyncSession) -> None:
    machine = await make_machine(db_session)
    product = await make_product(db_session)
    await make_slot(db_session, machine=machine, product=product, slot_code="B2")
    await make_slot(db_session, machine=machine, product=None, slot_code="A1")
    slots = (
        await db_session.scalars(
            select(MachineSlot)
            .where(MachineSlot.machine_id == machine.id)
            .order_by(MachineSlot.slot_code)
        )
    ).all()
    assert [s.slot_code for s in slots] == ["A1", "B2"]
