"""Expiry sweeper, Redis<->Postgres reconciliation, and dispense-vs-expiry races."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from redis.asyncio import Redis
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    MachineSlot,
    Order,
    PaymentStatus,
    Reservation,
    ReservationStatus,
    StockMovement,
    StockMovementReason,
)
from app.services import dispensing, hold_store
from app.services.hold_reconciliation import ReconcileReport, reconcile_all, reconcile_slot
from app.services.hold_store import ExistingHold
from app.services.inventory import InsufficientStockError, record_stock_movement
from app.services.payments import MockPaymentProvider
from app.services.reservation_expiry import expire_due_reservations
from app.services.reservations import load_slot_state
from tests.factories import (
    make_machine,
    make_order,
    make_product,
    make_reservation,
    make_slot,
    make_user,
)
from tests.world import World

pytestmark = pytest.mark.integration


async def _slot(session: AsyncSession, quantity: int = 5) -> MachineSlot:
    product = await make_product(session)
    machine = await make_machine(session)
    return await make_slot(session, machine=machine, product=product, quantity=quantity)


async def _hold(
    session: AsyncSession,
    redis: Redis,
    slot: MachineSlot,
    *,
    expires_in: timedelta = timedelta(minutes=10),
    status: ReservationStatus = ReservationStatus.HELD,
    quantity: int = 1,
) -> Reservation:
    """A reservation in Postgres with the matching hold in Redis."""
    user = await make_user(session)
    r = await make_reservation(
        session, user=user, slot=slot, quantity=quantity, status=status, expires_in=expires_in
    )
    await hold_store.reserve(
        redis, slot.id, r.id, quantity, 900, lambda sid: load_slot_state(session, sid)
    )
    return r


async def _reconcile(session: AsyncSession, redis: Redis, slot: MachineSlot) -> ReconcileReport:
    report = ReconcileReport()
    await reconcile_slot(session, redis, slot.id, report)
    return report


# --- expiry sweeper ---------------------------------------------------------------


async def test_sweeper_expires_only_lapsed_holds(db_session: AsyncSession, redis: Redis) -> None:
    slot = await _slot(db_session)
    user = await make_user(db_session)
    lapsed_held = await make_reservation(
        db_session, user=user, slot=slot, expires_in=timedelta(seconds=-5)
    )
    lapsed_paid = await make_reservation(
        db_session, user=user, slot=slot, status=ReservationStatus.PAID,
        expires_in=timedelta(seconds=-5),
    )  # fmt: skip
    valid = await make_reservation(db_session, user=user, slot=slot)
    cancelled = await make_reservation(
        db_session, user=user, slot=slot, status=ReservationStatus.CANCELLED,
        expires_in=timedelta(seconds=-5),
    )  # fmt: skip

    expired = await expire_due_reservations(db_session, redis)

    assert set(expired) == {lapsed_held.id, lapsed_paid.id}
    statuses = dict(
        (await db_session.execute(select(Reservation.id, Reservation.status))).tuples().all()
    )
    assert statuses[lapsed_held.id] is ReservationStatus.EXPIRED
    assert statuses[lapsed_paid.id] is ReservationStatus.EXPIRED
    assert statuses[valid.id] is ReservationStatus.HELD
    assert statuses[cancelled.id] is ReservationStatus.CANCELLED
    assert await expire_due_reservations(db_session, redis) == []  # idempotent


async def test_sweeper_tidies_redis(db_session: AsyncSession, redis: Redis) -> None:
    slot = await _slot(db_session)
    r = await _hold(db_session, redis, slot, expires_in=timedelta(seconds=-1))
    # Make Redis still believe in the hold (it was granted with a 900 s TTL above).
    assert r.id in (await hold_store.read_state(redis, slot.id)).holds
    await expire_due_reservations(db_session, redis)
    assert r.id not in (await hold_store.read_state(redis, slot.id)).holds


async def test_sweeper_batches(db_session: AsyncSession, redis: Redis) -> None:
    slot = await _slot(db_session)
    user = await make_user(db_session)
    for _ in range(7):
        await make_reservation(db_session, user=user, slot=slot, expires_in=timedelta(seconds=-1))
    # Regression: an `IN (SELECT ... LIMIT n FOR UPDATE SKIP LOCKED)` sweep
    # expired all 7 here (plan-dependent; seen on a never-analysed table).
    assert len(await expire_due_reservations(db_session, redis, batch_size=5)) == 5
    assert len(await expire_due_reservations(db_session, redis, batch_size=5)) == 2


# --- reconciliation ---------------------------------------------------------------


async def test_reconcile_initialises_unknown_slot(db_session: AsyncSession, redis: Redis) -> None:
    slot = await _slot(db_session, quantity=4)
    user = await make_user(db_session)
    await make_reservation(db_session, user=user, slot=slot, quantity=3)
    report = await _reconcile(db_session, redis, slot)
    assert report.slots_initialised == 1
    snap = await hold_store.snapshot(redis, slot.id)
    assert (snap.physical, snap.held, snap.available) == (4, 3, 1)


async def test_reconcile_restores_lost_hold(db_session: AsyncSession, redis: Redis) -> None:
    slot = await _slot(db_session, quantity=2)
    r = await _hold(db_session, redis, slot)
    await hold_store.release(redis, slot.id, r.id)  # Redis lost it
    assert (await hold_store.snapshot(redis, slot.id)).available == 2  # would oversell

    report = await _reconcile(db_session, redis, slot)

    assert report.holds_added == 1
    assert (await hold_store.snapshot(redis, slot.id)).available == 1


async def test_reconcile_drops_hold_of_finished_reservation(
    db_session: AsyncSession, redis: Redis
) -> None:
    """A cancel committed in Postgres but whose Redis release failed."""
    slot = await _slot(db_session, quantity=1)
    r = await _hold(db_session, redis, slot)
    r.status = ReservationStatus.CANCELLED
    await db_session.flush()
    assert (await hold_store.snapshot(redis, slot.id)).available == 0  # unit stuck

    report = await _reconcile(db_session, redis, slot)

    assert report.holds_removed == 1
    assert (await hold_store.snapshot(redis, slot.id)).available == 1


async def test_reconcile_orphans_respect_grace_period(
    db_session: AsyncSession, redis: Redis
) -> None:
    """Holds with no Postgres row: old ones are orphans, young ones may be in flight."""
    slot = await _slot(db_session, quantity=5)
    await hold_store.initialise(redis, slot.id, await load_slot_state(db_session, slot.id))
    now = datetime.now(UTC)
    stale = ExistingHold(uuid.uuid4(), 1, now + timedelta(seconds=900 - 600))  # made 10 min ago
    fresh = ExistingHold(uuid.uuid4(), 1, now + timedelta(seconds=900 - 5))  # made 5 s ago
    await hold_store.apply_reconciliation(
        redis, slot.id, expected_quantity=5, new_quantity=5, remove=[], add=[stale, fresh]
    )

    report = await _reconcile(db_session, redis, slot)

    holds = (await hold_store.read_state(redis, slot.id)).holds
    assert report.holds_removed == 1
    assert stale.reservation_id not in holds
    assert fresh.reservation_id in holds


async def test_reconcile_fixes_quantity_drift(db_session: AsyncSession, redis: Redis) -> None:
    slot = await _slot(db_session, quantity=5)
    await hold_store.initialise(redis, slot.id, await load_slot_state(db_session, slot.id))
    await hold_store.apply_stock_delta(redis, slot.id, 4)  # mirror says 9: would oversell
    report = await _reconcile(db_session, redis, slot)
    assert report.quantities_fixed == 1
    assert (await hold_store.snapshot(redis, slot.id)).physical == 5


async def test_reconcile_quantity_is_compare_and_set(redis: Redis) -> None:
    """A stock change landing between read and write is never overwritten."""
    slot = uuid.uuid4()
    await hold_store.initialise(redis, slot, hold_store.SlotState(5, []))
    stale_read = (await hold_store.read_state(redis, slot)).physical  # 5
    await hold_store.apply_stock_delta(redis, slot, -1)  # a dispense lands: 4
    result = await hold_store.apply_reconciliation(
        redis, slot, expected_quantity=stale_read, new_quantity=7, remove=[], add=[]
    )
    assert not result.quantity_updated
    assert (await hold_store.snapshot(redis, slot)).physical == 4


async def test_reconcile_never_drops_concurrently_added_hold(redis: Redis) -> None:
    slot = uuid.uuid4()
    await hold_store.initialise(redis, slot, hold_store.SlotState(5, []))
    new = uuid.uuid4()
    await hold_store.reserve(redis, slot, new, 1, 900, lambda _s: None)  # type: ignore[arg-type,return-value]
    # A diff computed before `new` existed carries no instruction about it.
    await hold_store.apply_reconciliation(
        redis, slot, expected_quantity=5, new_quantity=5, remove=[uuid.uuid4()], add=[]
    )
    assert new in (await hold_store.read_state(redis, slot)).holds


async def test_redis_wipe_and_full_reconcile(db_session: AsyncSession, redis: Redis) -> None:
    """Redis restarted without persistence: reconciliation rebuilds every slot."""
    slots = [await _slot(db_session, quantity=q) for q in (3, 2)]
    for slot in slots:
        await _hold(db_session, redis, slot)
    await redis.flushdb()

    report = await reconcile_all(db_session, redis)

    assert report.slots_initialised >= 2
    for slot in slots:
        snap = await hold_store.snapshot(redis, slot.id)
        assert snap.held == 1
        assert snap.available == slot.quantity - 1
    # A second pass finds nothing to do.
    again = await reconcile_all(db_session, redis)
    assert (again.holds_added, again.holds_removed, again.quantities_fixed) == (0, 0, 0)


# --- dispensing ---------------------------------------------------------------------


async def test_dispense_updates_ledger_redis_and_captures(
    db_session: AsyncSession, redis: Redis, provider: MockPaymentProvider
) -> None:
    slot = await _slot(db_session, quantity=5)
    r = await _hold(db_session, redis, slot, quantity=2, status=ReservationStatus.PAID)
    order = await make_order(db_session, reservation=r)

    done = await dispensing.complete_dispense(db_session, redis, provider, r.id)

    assert done.status is ReservationStatus.DISPENSED
    await db_session.refresh(slot)
    assert slot.quantity == 3
    movement = await db_session.scalar(
        select(StockMovement).where(StockMovement.reservation_id == r.id)
    )
    assert movement is not None
    assert (movement.delta, movement.reason) == (-2, StockMovementReason.DISPENSE)
    snap = await hold_store.snapshot(redis, slot.id)
    assert (snap.physical, snap.held, snap.available) == (3, 0, 3)
    await db_session.refresh(order)
    assert order.payment_status is PaymentStatus.CAPTURED
    assert order.payment_reference in provider.captured
    # Idempotent: a retried "dispensed" message changes nothing.
    await dispensing.complete_dispense(db_session, redis, provider, r.id)
    await db_session.refresh(slot)
    assert slot.quantity == 3


@pytest.mark.parametrize(
    ("status", "expires_in", "reported"),
    [(ReservationStatus.PAID, timedelta(seconds=-1), ReservationStatus.EXPIRED),
     (ReservationStatus.EXPIRED, timedelta(seconds=-60), ReservationStatus.EXPIRED),
     (ReservationStatus.CANCELLED, timedelta(minutes=5), ReservationStatus.CANCELLED),
     (ReservationStatus.HELD, timedelta(minutes=5), ReservationStatus.HELD)],
    ids=["lapsed", "expired", "cancelled", "unpaid"],
)  # fmt: skip
async def test_dispense_refused_for_inactive_reservations(
    db_session: AsyncSession,
    redis: Redis,
    provider: MockPaymentProvider,
    status: ReservationStatus,
    expires_in: timedelta,
    reported: ReservationStatus,
) -> None:
    slot = await _slot(db_session, quantity=5)
    user = await make_user(db_session)
    r = await make_reservation(
        db_session, user=user, slot=slot, status=status, expires_in=expires_in
    )
    with pytest.raises(dispensing.ReservationNotDispensableError) as info:
        await dispensing.complete_dispense(db_session, redis, provider, r.id)
    assert info.value.status is reported
    await db_session.refresh(slot)
    assert slot.quantity == 5


async def test_dispense_refused_when_ledger_disagrees(
    db_session: AsyncSession, redis: Redis, provider: MockPaymentProvider
) -> None:
    """The ledger is the final guard: no dispensing of stock that is not there."""
    slot = await _slot(db_session, quantity=1)
    r = await _hold(db_session, redis, slot, status=ReservationStatus.PAID)
    await db_session.execute(
        update(MachineSlot).where(MachineSlot.id == slot.id).values(quantity=0)
    )
    with pytest.raises(InsufficientStockError):
        await dispensing.complete_dispense(db_session, redis, provider, r.id)


# --- races: dispense vs expiry, on real concurrent connections ----------------------


async def _world_reservation(
    world: World, slot: MachineSlot, expires_in: timedelta, redis: Redis
) -> Reservation:
    (user,) = await world.users(1)
    r = Reservation(
        user_id=user.id, slot_id=slot.id, quantity=1, unit_price_cents=500,
        status=ReservationStatus.PAID, expires_at=datetime.now(UTC) + expires_in,
    )  # fmt: skip
    await world.add(r)
    await world.add(
        Order(reservation_id=r.id, user_id=user.id, amount_cents=500, currency="RON",
              payment_status=PaymentStatus.AUTHORIZED, payment_reference=f"mock_{r.id.hex}")
    )  # fmt: skip
    async with world.sessions() as session:
        await hold_store.reserve(
            redis, slot.id, r.id, 1, 900, lambda sid: load_slot_state(session, sid)
        )
    return r


async def _status_and_stock(world: World, r: Reservation) -> tuple[ReservationStatus, int]:
    async with world.sessions() as session:
        status = await session.scalar(select(Reservation.status).where(Reservation.id == r.id))
        quantity = await session.scalar(
            select(MachineSlot.quantity).where(MachineSlot.id == r.slot_id)
        )
        assert status is not None and quantity is not None
        return status, quantity


async def test_dispense_in_flight_survives_expiry(
    world: World, redis: Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hold lapses while the machine is dispensing: the dispense still wins."""
    slot = await world.slot(quantity=3)
    r = await _world_reservation(world, slot, timedelta(seconds=1), redis)

    real = record_stock_movement

    async def slow_motor(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(2)  # the hold's expiry passes in here
        return await real(*args, **kwargs)

    monkeypatch.setattr(dispensing, "record_stock_movement", slow_motor)

    async def dispense() -> None:
        async with world.sessions() as session:
            await dispensing.complete_dispense(session, redis, MockPaymentProvider(), r.id)

    task = asyncio.create_task(dispense())
    await asyncio.sleep(1.5)  # now past expires_at, row still locked by the dispense
    async with world.sessions() as session:
        swept = await expire_due_reservations(session, redis)
    await task

    assert r.id not in swept  # SKIP LOCKED: the sweeper stepped aside
    assert await _status_and_stock(world, r) == (ReservationStatus.DISPENSED, 2)


async def test_expiry_first_refuses_dispense(world: World, redis: Redis) -> None:
    slot = await world.slot(quantity=3)
    r = await _world_reservation(world, slot, timedelta(seconds=-1), redis)
    async with world.sessions() as session:
        assert r.id in await expire_due_reservations(session, redis)
    async with world.sessions() as session:
        with pytest.raises(dispensing.ReservationNotDispensableError):
            await dispensing.complete_dispense(session, redis, MockPaymentProvider(), r.id)
    assert await _status_and_stock(world, r) == (ReservationStatus.EXPIRED, 3)


@pytest.mark.parametrize("round_", range(5))
async def test_concurrent_dispenses_and_sweeps(world: World, redis: Redis, round_: int) -> None:
    """Many reservations at the expiry boundary, dispensed and swept concurrently:
    each ends exactly once, stock matches the ledger, and Redis agrees."""
    slot = await world.slot(quantity=10)
    reservations = [
        await _world_reservation(world, slot, timedelta(milliseconds=offset), redis)
        for offset in (-300, -100, 100, 300, 500, 800, 1200, 1500)
    ]

    async def dispense(r: Reservation) -> str:
        async with world.sessions() as session:
            try:
                await dispensing.complete_dispense(session, redis, MockPaymentProvider(), r.id)
                return "dispensed"
            except dispensing.ReservationNotDispensableError:
                return "refused"

    async def sweep() -> None:
        for _ in range(5):
            async with world.sessions() as session:
                await expire_due_reservations(session, redis)
            await asyncio.sleep(0.2)

    await asyncio.sleep(0.4)
    outcomes = await asyncio.gather(*(dispense(r) for r in reservations), sweep(), sweep())
    dispensed = outcomes[: len(reservations)].count("dispensed")

    async with world.sessions() as session:
        statuses = list(
            await session.scalars(select(Reservation.status).where(Reservation.slot_id == slot.id))
        )
        ledger = await session.scalar(
            select(func.count()).where(
                StockMovement.slot_id == slot.id,
                StockMovement.reason == StockMovementReason.DISPENSE,
            )
        )
        quantity = await session.scalar(
            select(MachineSlot.quantity).where(MachineSlot.id == slot.id)
        )
        report = await reconcile_all(session, redis)
    assert statuses.count(ReservationStatus.DISPENSED) == dispensed == ledger
    assert all(s in (ReservationStatus.DISPENSED, ReservationStatus.EXPIRED) for s in statuses)
    assert quantity == 10 - dispensed
    assert report.quantities_fixed == 0  # Redis mirror was already right
    snap = await hold_store.snapshot(redis, slot.id)
    assert (snap.physical, snap.held) == (10 - dispensed, 0)
