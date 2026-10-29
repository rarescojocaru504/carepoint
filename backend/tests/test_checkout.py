"""Checkout, payment settlement, order history and the reservation state machine."""

import asyncio
import uuid
from datetime import timedelta

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import create_access_token
from app.main import app
from app.models import MachineSlot, Order, PaymentStatus, Reservation, ReservationStatus, User
from app.services import dispensing, hold_store
from app.services.payment_settlement import settle_pending
from app.services.payments import (
    MOCK_DECLINED,
    MOCK_OK,
    MOCK_PROVIDER_ERROR,
    MockPaymentProvider,
    get_payment_provider,
)
from app.services.reservation_expiry import expire_due_reservations
from tests.factories import make_machine, make_product, make_reservation, make_slot, make_user
from tests.world import World

pytestmark = pytest.mark.integration


def _auth(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user.id, user.role)[0]}"}


async def _setup(session: AsyncSession, quantity: int = 5) -> tuple[User, MachineSlot]:
    user = await make_user(session)
    product = await make_product(session, price_cents=1250)
    machine = await make_machine(session)
    return user, await make_slot(session, machine=machine, product=product, quantity=quantity)


async def _reserve(api: httpx.AsyncClient, user: User, slot: MachineSlot, qty: int = 1) -> str:
    response = await api.post(
        "/api/v1/reservations", json={"slot_id": str(slot.id), "quantity": qty},
        headers=_auth(user),
    )  # fmt: skip
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def _checkout(
    api: httpx.AsyncClient, user: User, rid: str, method: str = MOCK_OK
) -> httpx.Response:
    return await api.post(
        f"/api/v1/reservations/{rid}/checkout",
        json={"payment_method": method},
        headers=_auth(user),
    )


async def _order(session: AsyncSession, rid: str) -> Order | None:
    """Fresh from the database (another request may have changed it)."""
    order: Order | None = await session.scalar(
        select(Order)
        .where(Order.reservation_id == uuid.UUID(rid))
        .execution_options(populate_existing=True)
    )
    return order


# --- checkout -----------------------------------------------------------------------


async def test_checkout_authorizes_and_marks_paid(
    api: httpx.AsyncClient, db_session: AsyncSession, redis: Redis, provider: MockPaymentProvider
) -> None:
    user, slot = await _setup(db_session)
    rid = await _reserve(api, user, slot, qty=2)

    response = await _checkout(api, user, rid)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["reservation"]["status"] == "paid"
    order = body["order"]
    expected = (2500, "RON", "authorized")
    assert (order["amount_cents"], order["currency"], order["payment_status"]) == expected
    assert order["reservation_status"] == "paid"
    # Paid reservations keep holding their stock until dispensed or released.
    assert (await hold_store.snapshot(redis, slot.id)).held == 2


async def test_checkout_is_idempotent(
    api: httpx.AsyncClient, db_session: AsyncSession, provider: MockPaymentProvider
) -> None:
    user, slot = await _setup(db_session)
    rid = await _reserve(api, user, slot)
    first = await _checkout(api, user, rid)
    again = await _checkout(api, user, rid)
    assert (first.status_code, again.status_code) == (201, 200)
    assert first.json()["order"]["id"] == again.json()["order"]["id"]
    assert await db_session.scalar(select(func.count()).select_from(Order)) == 1


async def test_declined_payment_keeps_hold_and_allows_retry(
    api: httpx.AsyncClient, db_session: AsyncSession, provider: MockPaymentProvider
) -> None:
    user, slot = await _setup(db_session)
    rid = await _reserve(api, user, slot)

    declined = await _checkout(api, user, rid, MOCK_DECLINED)
    assert declined.status_code == 402
    assert declined.json()["detail"]["code"] == "payment_declined"
    assert await _order(db_session, rid) is None
    status = (await api.get(f"/api/v1/reservations/{rid}", headers=_auth(user))).json()["status"]
    assert status == "held"

    assert (await _checkout(api, user, rid, MOCK_OK)).status_code == 201


async def test_provider_outage_at_checkout(
    api: httpx.AsyncClient, db_session: AsyncSession, provider: MockPaymentProvider
) -> None:
    user, slot = await _setup(db_session)
    rid = await _reserve(api, user, slot)
    response = await _checkout(api, user, rid, MOCK_PROVIDER_ERROR)
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "payment_unavailable"
    assert await _order(db_session, rid) is None


@pytest.mark.parametrize(
    ("method", "expected"),
    [("4111111111111111", 422), ("pm_", 422), ("PM_CARD_OK", 422), ("pm_card_unknown", 402)],
)
async def test_payment_method_validation(
    api: httpx.AsyncClient,
    db_session: AsyncSession,
    provider: MockPaymentProvider,
    method: str,
    expected: int,
) -> None:
    """Only provider tokens are accepted, never raw card numbers."""
    user, slot = await _setup(db_session)
    rid = await _reserve(api, user, slot)
    assert (await _checkout(api, user, rid, method)).status_code == expected


async def test_checkout_someone_elses_reservation(
    api: httpx.AsyncClient, db_session: AsyncSession, provider: MockPaymentProvider
) -> None:
    alice, slot = await _setup(db_session)
    bob = await make_user(db_session)
    rid = await _reserve(api, alice, slot)
    assert (await _checkout(api, bob, rid)).status_code == 404


async def test_checkout_rate_limited(
    api: httpx.AsyncClient,
    db_session: AsyncSession,
    provider: MockPaymentProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Blunts card testing: many attempts with different tokens get throttled."""
    monkeypatch.setattr(get_settings(), "rate_limit_checkout_user", "3/60")
    user, slot = await _setup(db_session)
    rid = await _reserve(api, user, slot)
    statuses = [(await _checkout(api, user, rid, MOCK_DECLINED)).status_code for _ in range(4)]
    assert statuses == [402, 402, 402, 429]


# --- settlement: capture on dispense, void on cancel / expiry ----------------------


async def test_cancel_paid_reservation_voids(
    api: httpx.AsyncClient, db_session: AsyncSession, provider: MockPaymentProvider
) -> None:
    user, slot = await _setup(db_session, quantity=1)
    rid = await _reserve(api, user, slot)
    await _checkout(api, user, rid)

    assert (await api.delete(f"/api/v1/reservations/{rid}", headers=_auth(user))).status_code == 204

    order = await _order(db_session, rid)
    assert order is not None and order.payment_status is PaymentStatus.VOIDED
    assert order.payment_reference in provider.voided
    detail = (await api.get(f"/api/v1/machines/{slot.machine_id}")).json()
    assert detail["slots"][0]["available"] == 1


async def test_void_retried_after_provider_outage(
    api: httpx.AsyncClient, db_session: AsyncSession, provider: MockPaymentProvider
) -> None:
    user, slot = await _setup(db_session)
    rid = await _reserve(api, user, slot)
    await _checkout(api, user, rid)
    provider.fail_next_settlements = 1

    assert (await api.delete(f"/api/v1/reservations/{rid}", headers=_auth(user))).status_code == 204
    order = await _order(db_session, rid)
    assert order is not None and order.payment_status is PaymentStatus.AUTHORIZED  # deferred

    report = await settle_pending(db_session, provider)
    assert (report.voided, report.failed) == (1, 0)
    order = await _order(db_session, rid)
    assert order is not None and order.payment_status is PaymentStatus.VOIDED


async def test_capture_retried_after_provider_outage(
    api: httpx.AsyncClient, db_session: AsyncSession, redis: Redis, provider: MockPaymentProvider
) -> None:
    """The item is already out of the machine: the capture must eventually happen."""
    user, slot = await _setup(db_session)
    rid = await _reserve(api, user, slot)
    await _checkout(api, user, rid)
    provider.fail_next_settlements = 2

    await dispensing.complete_dispense(db_session, redis, provider, uuid.UUID(rid))
    order = await _order(db_session, rid)
    assert order is not None and order.payment_status is PaymentStatus.AUTHORIZED

    assert (await settle_pending(db_session, provider)).failed == 1  # still down
    assert (await settle_pending(db_session, provider)).captured == 1  # back up
    order = await _order(db_session, rid)
    assert order is not None and order.payment_status is PaymentStatus.CAPTURED


async def test_expired_paid_reservation_is_voided(
    db_session: AsyncSession, redis: Redis, provider: MockPaymentProvider
) -> None:
    user, slot = await _setup(db_session)
    r = await make_reservation(
        db_session, user=user, slot=slot, status=ReservationStatus.PAID,
        expires_in=timedelta(seconds=-1),
    )  # fmt: skip
    order = Order(
        reservation_id=r.id,
        user_id=user.id,
        amount_cents=999,
        currency="RON",
        payment_status=PaymentStatus.AUTHORIZED,
        payment_reference="mock_exp",
    )
    db_session.add(order)
    await db_session.flush()

    await expire_due_reservations(db_session, redis)
    report = await settle_pending(db_session, provider)

    assert report.voided == 1
    await db_session.refresh(order)
    assert order.payment_status is PaymentStatus.VOIDED


async def test_one_failure_does_not_block_other_settlements(
    api: httpx.AsyncClient, db_session: AsyncSession, provider: MockPaymentProvider
) -> None:
    """During a flaky outage, one failed void must not hold up everyone else's."""
    user, slot = await _setup(db_session, quantity=10)
    rids = []
    for _ in range(3):
        rid = await _reserve(api, user, slot)
        await _checkout(api, user, rid)
        rids.append(rid)
    provider.fail_next_settlements = 3  # the immediate voids on cancel all fail
    for rid in rids:
        await api.delete(f"/api/v1/reservations/{rid}", headers=_auth(user))

    provider.fail_next_settlements = 1  # the first retry fails, the rest succeed
    report = await settle_pending(db_session, provider)

    assert (report.failed, report.voided) == (1, 2)
    assert (await settle_pending(db_session, provider)).voided == 1


async def test_settlement_leaves_open_reservations_alone(
    api: httpx.AsyncClient, db_session: AsyncSession, provider: MockPaymentProvider
) -> None:
    user, slot = await _setup(db_session)
    rid = await _reserve(api, user, slot)
    await _checkout(api, user, rid)
    report = await settle_pending(db_session, provider)
    assert (report.captured, report.voided, report.failed) == (0, 0, 0)
    order = await _order(db_session, rid)
    assert order is not None and order.payment_status is PaymentStatus.AUTHORIZED


# --- the state machine --------------------------------------------------------------

TRANSITIONS: list[tuple[ReservationStatus, str, ReservationStatus | None]] = [
    # (from, action, to — None means the action must be refused, state unchanged)
    (ReservationStatus.HELD, "checkout", ReservationStatus.PAID),
    (ReservationStatus.HELD, "cancel", ReservationStatus.CANCELLED),
    (ReservationStatus.HELD, "expire", ReservationStatus.EXPIRED),
    (ReservationStatus.HELD, "dispense", None),  # unpaid
    (ReservationStatus.PAID, "dispense", ReservationStatus.DISPENSED),
    (ReservationStatus.PAID, "cancel", ReservationStatus.CANCELLED),
    (ReservationStatus.PAID, "expire", ReservationStatus.EXPIRED),
    (ReservationStatus.PAID, "checkout", ReservationStatus.PAID),  # idempotent
    (ReservationStatus.DISPENSED, "checkout", None),
    (ReservationStatus.DISPENSED, "cancel", None),
    (ReservationStatus.DISPENSED, "dispense", ReservationStatus.DISPENSED),  # idempotent
    (ReservationStatus.EXPIRED, "checkout", None),
    (ReservationStatus.EXPIRED, "cancel", None),
    (ReservationStatus.EXPIRED, "dispense", None),
    (ReservationStatus.CANCELLED, "checkout", None),
    (ReservationStatus.CANCELLED, "dispense", None),
    (ReservationStatus.CANCELLED, "cancel", ReservationStatus.CANCELLED),  # idempotent
]


@pytest.mark.parametrize(
    ("start", "action", "end"), TRANSITIONS, ids=[f"{s.value}-{a}" for s, a, _ in TRANSITIONS]
)
async def test_state_machine(
    api: httpx.AsyncClient,
    db_session: AsyncSession,
    redis: Redis,
    provider: MockPaymentProvider,
    start: ReservationStatus,
    action: str,
    end: ReservationStatus | None,
) -> None:
    user, slot = await _setup(db_session, quantity=3)
    expires_in = timedelta(seconds=-1) if action == "expire" else timedelta(minutes=10)
    r = await make_reservation(
        db_session, user=user, slot=slot, status=start, expires_in=expires_in
    )
    if start in (ReservationStatus.PAID, ReservationStatus.DISPENSED):
        db_session.add(Order(reservation_id=r.id, user_id=user.id, amount_cents=999,
                             currency="RON", payment_status=PaymentStatus.AUTHORIZED,
                             payment_reference=f"mock_{r.id.hex}"))  # fmt: skip
        await db_session.flush()

    refused = False
    if action == "checkout":
        refused = (await _checkout(api, user, str(r.id))).status_code >= 400
    elif action == "cancel":
        response = await api.delete(f"/api/v1/reservations/{r.id}", headers=_auth(user))
        refused = response.status_code >= 400
    elif action == "dispense":
        try:
            await dispensing.complete_dispense(db_session, redis, provider, r.id)
        except dispensing.ReservationNotDispensableError:
            refused = True
    else:
        await expire_due_reservations(db_session, redis)

    # A column select always reads the current row, whatever the session cached.
    final = await db_session.scalar(select(Reservation.status).where(Reservation.id == r.id))
    if end is None:
        assert refused, f"{start.value} -> {action} must be refused"
        assert final is start
    else:
        assert not refused
        assert final is end


# --- order history ------------------------------------------------------------------


async def test_order_history(
    api: httpx.AsyncClient, db_session: AsyncSession, provider: MockPaymentProvider
) -> None:
    alice, slot = await _setup(db_session)
    bob = await make_user(db_session)
    first = await _reserve(api, alice, slot)
    await _checkout(api, alice, first)
    second = await _reserve(api, alice, slot, qty=2)
    await _checkout(api, alice, second)
    await api.delete(f"/api/v1/reservations/{second}", headers=_auth(alice))

    orders = (await api.get("/api/v1/orders", headers=_auth(alice))).json()
    # Newest first. (Both orders were created in this test's single transaction,
    # where Postgres now() is constant, so compare timestamps, not positions.)
    assert {o["reservation_id"] for o in orders} == {first, second}
    created = [o["created_at"] for o in orders]
    assert created == sorted(created, reverse=True)
    by_reservation = {o["reservation_id"]: o for o in orders}
    cancelled = by_reservation[second]
    assert (cancelled["payment_status"], cancelled["reservation_status"]) == (
        "voided",
        "cancelled",
    )
    assert (cancelled["quantity"], cancelled["amount_cents"]) == (2, 2500)
    assert (await api.get("/api/v1/orders", headers=_auth(bob))).json() == []

    paid_id = by_reservation[first]["id"]
    one = await api.get(f"/api/v1/orders/{paid_id}", headers=_auth(alice))
    assert one.status_code == 200 and one.json()["payment_status"] == "authorized"
    assert (await api.get(f"/api/v1/orders/{paid_id}", headers=_auth(bob))).status_code == 404
    assert (await api.get("/api/v1/orders")).status_code == 401


# --- races ---------------------------------------------------------------------------


async def _world_hold(world: World, redis: Redis) -> tuple[User, Reservation]:
    slot = await world.slot(quantity=3)
    (user,) = await world.users(1)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        rid = await _reserve(c, user, slot)
    async with world.sessions() as session:
        r = await session.get(Reservation, uuid.UUID(rid))
        assert r is not None
        return user, r


async def test_concurrent_checkouts_create_one_order(world: World, redis: Redis) -> None:
    """A double-tapped "pay" button: one authorization, one order."""
    provider = MockPaymentProvider()
    app.dependency_overrides[get_payment_provider] = lambda: provider
    user, r = await _world_hold(world, redis)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        responses = await asyncio.gather(*(_checkout(c, user, str(r.id)) for _ in range(6)))
    assert sorted(x.status_code for x in responses) == [200] * 5 + [201]
    assert len({x.json()["order"]["id"] for x in responses}) == 1
    async with world.sessions() as session:
        count = await session.scalar(
            select(func.count()).select_from(Order).where(Order.reservation_id == r.id)
        )
    assert count == 1


async def test_concurrent_checkout_and_cancel_end_consistent(world: World, redis: Redis) -> None:
    """Whichever wins, money and goods agree: cancelled => nothing charged."""
    provider = MockPaymentProvider()
    app.dependency_overrides[get_payment_provider] = lambda: provider
    for _ in range(5):
        user, r = await _world_hold(world, redis)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as c:
            await asyncio.gather(
                _checkout(c, user, str(r.id)),
                c.delete(f"/api/v1/reservations/{r.id}", headers=_auth(user)),
            )
        async with world.sessions() as session:
            await settle_pending(session, provider)
            status = await session.scalar(select(Reservation.status).where(Reservation.id == r.id))
            payment = await session.scalar(
                select(Order.payment_status).where(Order.reservation_id == r.id)
            )
        assert status is ReservationStatus.CANCELLED
        assert payment in (None, PaymentStatus.VOIDED)
