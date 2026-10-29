"""Operator dashboard API: access control, machines, stock changes,
reservations across customers, the catalogue and KPIs."""

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token
from app.models import (
    Machine,
    MachineEvent,
    MachineEventKind,
    PaymentStatus,
    Product,
    ReservationStatus,
    StockMovement,
    StockMovementReason,
    User,
    UserRole,
)
from app.services import hold_store
from app.services.hold_store import SlotState
from app.services.kpis import connected_share
from app.services.payments import MockPaymentProvider
from tests.factories import (
    make_machine,
    make_order,
    make_product,
    make_reservation,
    make_slot,
    make_user,
)

pytestmark = pytest.mark.integration

NOW = datetime.now(UTC)


def _auth(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user.id, user.role)[0]}"}


async def _operator(session: AsyncSession, role: UserRole = UserRole.OPERATOR) -> dict[str, str]:
    return _auth(await make_user(session, role=role))


async def _online_machine(session: AsyncSession, **overrides: Any) -> Machine:
    return await make_machine(session, last_seen_at=datetime.now(UTC), **overrides)


# --- access ---------------------------------------------------------------------

ADMIN_GETS = ["/api/v1/admin/machines", "/api/v1/admin/reservations",
              "/api/v1/admin/products", "/api/v1/admin/kpis"]  # fmt: skip


@pytest.mark.parametrize("path", ADMIN_GETS)
async def test_dashboard_is_for_operators_only(
    api: httpx.AsyncClient, db_session: AsyncSession, path: str
) -> None:
    customer = _auth(await make_user(db_session))
    assert (await api.get(path)).status_code == 401
    assert (await api.get(path, headers=customer)).status_code == 403
    assert (await api.get(path, headers=await _operator(db_session))).status_code == 200
    admin = await _operator(db_session, UserRole.ADMIN)
    assert (await api.get(path, headers=admin)).status_code == 200


async def test_customer_cannot_change_stock(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    machine = await _online_machine(db_session)
    slot = await make_slot(db_session, machine=machine, product=await make_product(db_session),
                           quantity=1)  # fmt: skip
    response = await api.post(
        "/api/v1/admin/stock-movements",
        json={"slot_id": str(slot.id), "reason": "restock", "delta": 5},
        headers=_auth(await make_user(db_session)),
    )
    assert response.status_code == 403
    await db_session.refresh(slot)
    assert slot.quantity == 1


# --- machines -------------------------------------------------------------------


async def test_machine_list_shows_status_and_stock_warnings(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    online = await _online_machine(db_session, serial_number="CP-ADM-0001")
    await make_machine(db_session, serial_number="CP-ADM-0002",
                               last_seen_at=NOW - timedelta(hours=1))  # fmt: skip
    await make_machine(
        db_session, serial_number="CP-ADM-0003", last_seen_at=NOW - timedelta(seconds=5),
        disconnected_at=NOW,
    )  # fmt: skip
    product = await make_product(db_session)
    await make_slot(
        db_session,
        machine=online,
        product=product,
        slot_code="A1",
        quantity=8,
        low_stock_threshold=2,
    )
    await make_slot(
        db_session,
        machine=online,
        product=product,
        slot_code="A2",
        quantity=2,
        low_stock_threshold=2,
    )
    sold_out = await make_slot(
        db_session, machine=online, product=product, slot_code="A3", quantity=0
    )
    await make_slot(db_session, machine=online, product=None, slot_code="B1")  # unassigned
    customer = await make_user(db_session)
    await make_reservation(db_session, user=customer, slot=sold_out, quantity=1)  # fmt: skip
    online.temperature_c = 21.5  # type: ignore[assignment]
    await db_session.flush()

    machines = {
        m["serial_number"]: m
        for m in (
            await api.get("/api/v1/admin/machines", headers=await _operator(db_session))
        ).json()
    }

    assert machines["CP-ADM-0001"]["online"] is True
    assert machines["CP-ADM-0002"]["online"] is False  # silent too long
    assert machines["CP-ADM-0003"]["online"] is False  # its connection closed
    m = machines["CP-ADM-0001"]
    assert (m["units"], m["low_stock_slots"], m["sold_out_slots"]) == (10, 1, 1)
    assert m["active_reservations"] == 1
    assert m["temperature_c"] == 21.5
    assert set(m["location"]) == {"lat", "lng"}


async def test_machine_detail_has_slots_with_holds_and_recent_events(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    machine = await _online_machine(db_session)
    slot = await make_slot(db_session, machine=machine, product=await make_product(db_session),
                           slot_code="A1", quantity=5)  # fmt: skip
    await make_reservation(db_session, user=await make_user(db_session), slot=slot, quantity=2)
    db_session.add(MachineEvent(machine_id=machine.id, kind=MachineEventKind.CONNECTED))
    await db_session.flush()
    headers = await _operator(db_session)

    detail = (await api.get(f"/api/v1/admin/machines/{machine.id}", headers=headers)).json()

    [a1] = detail["slots"]
    assert (a1["quantity"], a1["held"], a1["available"]) == (5, 2, 3)
    assert a1["product"]["sku"] == (await db_session.get(Product, slot.product_id)).sku  # type: ignore[union-attr]
    assert [e["kind"] for e in detail["recent_events"]] == ["connected"]
    missing = await api.get(f"/api/v1/admin/machines/{slot.id}", headers=headers)
    assert missing.status_code == 404


async def test_operators_pause_machines_but_only_admins_decommission(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    machine = await _online_machine(db_session)
    url = f"/api/v1/admin/machines/{machine.id}"
    operator, admin = await _operator(db_session), await _operator(db_session, UserRole.ADMIN)

    paused = await api.patch(url, json={"status": "maintenance"}, headers=operator)
    assert paused.json()["status"] == "maintenance"
    denied = await api.patch(url, json={"status": "decommissioned"}, headers=operator)
    assert denied.status_code == 403
    done = await api.patch(url, json={"status": "decommissioned"}, headers=admin)
    assert done.json()["status"] == "decommissioned"


# --- stock changes --------------------------------------------------------------


async def test_restock_is_recorded_with_its_operator_and_mirrored(
    api: httpx.AsyncClient, db_session: AsyncSession, redis: Redis
) -> None:
    machine = await _online_machine(db_session)
    slot = await make_slot(db_session, machine=machine, product=await make_product(db_session),
                           quantity=2, capacity=10)  # fmt: skip
    await hold_store.initialise(redis, slot.id, SlotState(physical_quantity=2, holds=[]))
    operator = await make_user(db_session, role=UserRole.OPERATOR)

    response = await api.post(
        "/api/v1/admin/stock-movements",
        json={"slot_id": str(slot.id), "reason": "restock", "delta": 6, "note": "weekly round"},
        headers=_auth(operator),
    )

    assert response.status_code == 201
    body = response.json()
    assert (body["delta"], body["quantity_after"], body["reason"]) == (6, 8, "restock")
    assert (body["actor_email"], body["note"]) == (operator.email, "weekly round")
    assert body["machine_notified"] is False  # no live connection in this test
    await db_session.refresh(slot)
    assert slot.quantity == 8
    assert (await hold_store.snapshot(redis, slot.id)).physical == 8

    history = (
        await api.get(f"/api/v1/admin/slots/{slot.id}/movements", headers=_auth(operator))
    ).json()
    assert history[0]["id"] == body["id"] and history[0]["actor_email"] == operator.email


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"reason": "restock", "delta": 20}, 409),  # over capacity
        ({"reason": "adjustment", "delta": -5, "note": "counted"}, 409),  # below zero
        ({"reason": "adjustment", "delta": -1}, 422),  # an adjustment needs a note
        ({"reason": "restock", "delta": -1}, 422),  # a restock only adds
        ({"reason": "restock", "delta": 0}, 422),
        ({"reason": "dispense", "delta": 1}, 422),  # system-only reasons
        ({"reason": "restock", "delta": "3"}, 422),
    ],
)
async def test_bad_stock_changes_are_refused(
    api: httpx.AsyncClient, db_session: AsyncSession, body: dict[str, Any], status: int
) -> None:
    machine = await _online_machine(db_session)
    slot = await make_slot(db_session, machine=machine, product=await make_product(db_session),
                           quantity=2, capacity=10)  # fmt: skip
    response = await api.post(
        "/api/v1/admin/stock-movements",
        json={"slot_id": str(slot.id), **body},
        headers=await _operator(db_session),
    )
    assert response.status_code == status
    await db_session.refresh(slot)
    assert slot.quantity == 2


async def test_stock_change_needs_the_machine_online(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    machine = await make_machine(db_session, last_seen_at=NOW - timedelta(hours=2))
    slot = await make_slot(db_session, machine=machine, product=await make_product(db_session),
                           quantity=2)  # fmt: skip

    response = await api.post(
        "/api/v1/admin/stock-movements",
        json={"slot_id": str(slot.id), "reason": "restock", "delta": 1},
        headers=await _operator(db_session),
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "machine_offline"
    assert (
        await db_session.scalar(select(StockMovement).where(StockMovement.slot_id == slot.id))
    ) is None


# --- reservations ---------------------------------------------------------------


async def test_reservations_across_customers_with_filters(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    machine = await _online_machine(db_session)
    slot = await make_slot(db_session, machine=machine, product=await make_product(db_session),
                           quantity=9)  # fmt: skip
    alice, bob = await make_user(db_session), await make_user(db_session)
    held = await make_reservation(db_session, user=alice, slot=slot)
    paid = await make_reservation(db_session, user=bob, slot=slot, status=ReservationStatus.PAID)
    await make_order(db_session, reservation=paid)
    lapsed = await make_reservation(db_session, user=bob, slot=slot,
                                    expires_in=timedelta(seconds=-5))  # fmt: skip
    done = await make_reservation(db_session, user=alice, slot=slot,
                                  status=ReservationStatus.DISPENSED)  # fmt: skip
    headers = await _operator(db_session)

    async def ids(query: str) -> set[str]:
        response = await api.get(f"/api/v1/admin/reservations?machine_id={machine.id}{query}",
                                 headers=headers)  # fmt: skip
        return {r["id"] for r in response.json()}

    assert await ids("") == {str(r.id) for r in (held, paid, lapsed, done)}
    assert await ids("&status=active") == {str(held.id), str(paid.id)}
    assert await ids("&status=expired") == {str(lapsed.id)}  # lapsed, not yet swept
    assert await ids("&status=dispensed") == {str(done.id)}
    rows = (await api.get(f"/api/v1/admin/reservations?machine_id={machine.id}&status=paid",
                          headers=headers)).json()  # fmt: skip
    assert rows[0]["user_email"] == bob.email and rows[0]["payment_status"] == "authorized"


async def test_operator_cancels_a_customers_reservation(
    api: httpx.AsyncClient, db_session: AsyncSession, provider: MockPaymentProvider
) -> None:
    machine = await _online_machine(db_session)
    slot = await make_slot(db_session, machine=machine, product=await make_product(db_session),
                           quantity=3)  # fmt: skip
    paid = await make_reservation(db_session, user=await make_user(db_session), slot=slot,
                                  status=ReservationStatus.PAID)  # fmt: skip
    order = await make_order(db_session, reservation=paid)
    done = await make_reservation(db_session, user=await make_user(db_session), slot=slot,
                                  status=ReservationStatus.DISPENSED)  # fmt: skip
    headers = await _operator(db_session)

    response = await api.post(f"/api/v1/admin/reservations/{paid.id}/cancel", headers=headers)

    assert response.status_code == 204
    await db_session.refresh(paid)
    await db_session.refresh(order)
    assert paid.status is ReservationStatus.CANCELLED
    assert order.payment_status is PaymentStatus.VOIDED
    finished = await api.post(f"/api/v1/admin/reservations/{done.id}/cancel", headers=headers)
    assert finished.status_code == 409


# --- products -------------------------------------------------------------------


async def test_product_lifecycle(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    headers = await _operator(db_session)
    new = {"sku": "ADM-TEST-1", "name": "Lip balm", "category": "essentials",
           "price_cents": 1299, "currency": "RON"}  # fmt: skip

    created = await api.post("/api/v1/admin/products", json=new, headers=headers)
    assert created.status_code == 201
    product_id = created.json()["id"]
    assert (await api.post("/api/v1/admin/products", json=new, headers=headers)).status_code == 409

    updated = await api.patch(
        f"/api/v1/admin/products/{product_id}",
        json={"price_cents": 1499, "is_active": False},
        headers=headers,
    )
    assert (updated.json()["price_cents"], updated.json()["is_active"]) == (1499, False)
    listed = (await api.get("/api/v1/admin/products", headers=headers)).json()
    assert any(p["id"] == product_id and not p["is_active"] for p in listed)  # inactive included

    deleted = await api.delete(f"/api/v1/admin/products/{product_id}", headers=headers)
    assert deleted.status_code == 204
    gone = await api.delete(f"/api/v1/admin/products/{product_id}", headers=headers)
    assert gone.status_code == 404


async def test_product_in_a_slot_cannot_be_deleted(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    product = await make_product(db_session)
    await make_slot(db_session, machine=await make_machine(db_session), product=product)
    response = await api.delete(f"/api/v1/admin/products/{product.id}",
                                headers=await _operator(db_session))  # fmt: skip
    assert response.status_code == 409
    assert "deactivate" in response.json()["detail"]


# --- KPIs -----------------------------------------------------------------------


async def test_kpis_from_known_data(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    before = (await api.get("/api/v1/admin/kpis", headers=await _operator(db_session))).json()
    machine = await _online_machine(db_session)
    product = await make_product(db_session)
    slot = await make_slot(db_session, machine=machine, product=product, quantity=5,
                           slot_code="A1")  # fmt: skip
    await make_slot(db_session, machine=machine, product=product, quantity=0, slot_code="A2")
    user = await make_user(db_session)
    await make_reservation(db_session, user=user, slot=slot, status=ReservationStatus.DISPENSED)
    await make_reservation(db_session, user=user, slot=slot, expires_in=timedelta(seconds=-1))
    await make_reservation(db_session, user=user, slot=slot, status=ReservationStatus.CANCELLED)
    await make_reservation(db_session, user=user, slot=slot)  # still active: not finished

    after = (await api.get("/api/v1/admin/kpis", headers=await _operator(db_session))).json()

    assert after["reservations_24h"] - before["reservations_24h"] == 4
    assert after["reservations_7d"] - before["reservations_7d"] == 4
    assert after["expired_7d"] - before["expired_7d"] == 1
    assert after["stock_outs"] - before["stock_outs"] == 1
    assert after["machines_online"] - before["machines_online"] == 1
    assert after["machines_total"] - before["machines_total"] == 1
    assert after["pickup_rate_7d"] is not None and 0 <= after["pickup_rate_7d"] <= 1


async def test_pickup_rate_and_time_to_pickup(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    headers = await _operator(db_session)
    if (await api.get("/api/v1/admin/kpis", headers=headers)).json()["pickup_rate_7d"] is not None:
        pytest.skip("needs a database without finished reservations in the last 7 days")
    machine = await _online_machine(db_session)
    slot = await make_slot(db_session, machine=machine, product=await make_product(db_session),
                           quantity=5)  # fmt: skip
    user = await make_user(db_session)
    dispensed = await make_reservation(db_session, user=user, slot=slot,
                                       status=ReservationStatus.DISPENSED)  # fmt: skip
    db_session.add(StockMovement(slot_id=slot.id, delta=-1, quantity_after=4,
                                 reason=StockMovementReason.DISPENSE,
                                 reservation_id=dispensed.id))  # fmt: skip
    await make_reservation(db_session, user=user, slot=slot, status=ReservationStatus.CANCELLED)
    await make_reservation(db_session, user=user, slot=slot, expires_in=timedelta(seconds=-1))
    await make_reservation(db_session, user=user, slot=slot)  # active: not finished yet
    await db_session.flush()

    kpis = (await api.get("/api/v1/admin/kpis", headers=headers)).json()

    assert kpis["pickup_rate_7d"] == pytest.approx(1 / 3)  # 1 dispensed of 3 finished
    # Rows are stamped by the database's now(), which is fixed within this
    # test's transaction: hold and pickup share one timestamp.
    assert kpis["avg_hold_to_pickup_seconds_7d"] == 0


START = datetime(2026, 10, 1, tzinfo=UTC)
END = START + timedelta(hours=10)
WINDOW = timedelta(seconds=90)


def _at(hours: float) -> datetime:
    return START + timedelta(hours=hours)


@pytest.mark.parametrize(
    ("events", "initially", "last_seen", "share"),
    [
        ([], True, END, 1.0),
        ([], False, None, 0.0),
        ([(_at(2), "disconnected"), (_at(7), "connected")], True, END, 0.5),
        ([(_at(5), "connected")], False, END, 0.5),
        # Connected, then crashed silently at 4 h: up until last seen + window.
        ([(_at(1), "connected")], False, _at(4), (3 + 90 / 3600) / 10),
        # Events before the window are clamped to its start.
        ([(START - timedelta(hours=3), "connected")], False, END, 1.0),
    ],
)
def test_connected_share(
    events: list[tuple[datetime, str]], initially: bool, last_seen: datetime | None, share: float
) -> None:
    computed = connected_share(
        [(at, MachineEventKind(kind)) for at, kind in events],
        initially_connected=initially, start=START, end=END, last_seen_at=last_seen,
        online_window=WINDOW,
    )  # fmt: skip
    assert computed == pytest.approx(share)
