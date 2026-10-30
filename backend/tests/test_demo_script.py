"""The demo scenario script (ADR-032): simulated customers through the real API."""

import json
import random
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from sqlalchemy import select, update

from app.core.config import get_settings
from app.core.security import hash_password
from app.main import app
from app.models import (
    Machine,
    MachineSlot,
    Order,
    PaymentStatus,
    Reservation,
    ReservationStatus,
    StockMovement,
    StockMovementReason,
    User,
    UserRole,
)
from app.scripts import demo
from app.scripts.demo import SCENARIOS, Demo, Options
from tests.world import World

pytestmark = pytest.mark.integration

OPERATOR_PASSWORD = "operator-password-123"


class FakeKiosk:
    """The simulator's kiosk API: lists machines and records codes typed in."""

    def __init__(self) -> None:
        self.machines: list[dict[str, Any]] = []
        self.typed: list[tuple[str, str]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/machines":
            return httpx.Response(200, json=self.machines)
        if request.method == "POST" and request.url.path.endswith("/verify"):
            serial = request.url.path.split("/")[3]
            self.typed.append((serial, json.loads(request.content)["credential"]))
            return httpx.Response(200, json={"ok": True, "refusal": None, "command_id": "c1"})
        return httpx.Response(404)


async def _no_wait(_: float) -> None:
    return None


async def _online_machine(
    world: World, slots: dict[str, int | None]
) -> tuple[Machine, dict[str, MachineSlot]]:
    machine, _, made = await world.machine(slots)
    async with world.sessions() as session:
        await session.execute(
            update(Machine).where(Machine.id == machine.id).values(last_seen_at=datetime.now(UTC))
        )
        await session.commit()
    return machine, made


@pytest.fixture
async def setup(world: World) -> AsyncIterator[tuple[World, Options, FakeKiosk]]:
    domain = f"demo-{uuid.uuid4().hex[:8]}.example.com"
    operator = User(email=f"op@{domain}", password_hash=hash_password(OPERATOR_PASSWORD),
                    full_name="Operator", role=UserRole.OPERATOR)  # fmt: skip
    await world.add(operator)
    # The world's machines are in the Southern Ocean (tests/world.py).
    options = Options(lat=-60, lng=150, shoppers=3, operator_email=operator.email,
                      operator_password=OPERATOR_PASSWORD, shopper_domain=domain)  # fmt: skip
    kiosk = FakeKiosk()
    try:
        yield world, options, kiosk
    finally:
        # Shoppers are created through the API, so the world doesn't know them yet.
        async with world.sessions() as session:
            world.created.extend(
                await session.scalars(select(User).where(User.email.like(f"%@{domain}")))
            )


def _demo(
    options: Options, kiosk: FakeKiosk, lines: list[str]
) -> tuple[Demo, httpx.AsyncClient, httpx.AsyncClient]:
    api = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t/api/v1")
    sim = httpx.AsyncClient(transport=httpx.MockTransport(kiosk.handle), base_url="http://sim")
    rng = random.Random(1)  # noqa: S311  (repeatable test behaviour, not a secret)
    return Demo(api, sim, options, rng=rng, log=lines.append, sleep=_no_wait), api, sim


def _register(kiosk: FakeKiosk, machine: Machine, **faults: Any) -> None:
    kiosk.machines.append({"serial": machine.serial_number, "machine_id": str(machine.id),
                           "faults": {"offline": False, "jam": "off", **faults}})  # fmt: skip


async def _settle(d: Demo) -> None:
    for task in list(d.pending):
        await task


async def _reservation(world: World, rid: str) -> tuple[Reservation, Order | None]:
    async with world.sessions() as session:
        reservation = await session.get(Reservation, uuid.UUID(rid))
        order = await session.scalar(select(Order).where(Order.reservation_id == uuid.UUID(rid)))
        assert reservation is not None
        return reservation, order


async def test_a_customer_who_picks_up_types_their_code_at_that_machine(
    setup: tuple[World, Options, FakeKiosk],
) -> None:
    world, options, kiosk = setup
    machine, _ = await _online_machine(world, {"A1": 5})
    _register(kiosk, machine)
    lines: list[str] = []
    d, api, sim = _demo(options, kiosk, lines)
    async with api, sim:
        await d.setup()
        visit = await d.visit("pickup")
        await _settle(d)

    assert visit is not None and visit.code is not None
    assert kiosk.typed == [(machine.serial_number, visit.code)]
    reservation, order = await _reservation(world, visit.reservation_id)
    assert reservation.status is ReservationStatus.PAID  # the fake kiosk doesn't dispense
    assert order is not None and order.payment_status is PaymentStatus.AUTHORIZED
    assert visit.steps == ["reserved", "paid", "picked up"]
    assert any("reserved and paid" in line for line in lines)


async def test_a_customer_who_cancels_is_not_charged(
    setup: tuple[World, Options, FakeKiosk],
) -> None:
    world, options, kiosk = setup
    machine, _ = await _online_machine(world, {"A1": 5})
    _register(kiosk, machine)
    d, api, sim = _demo(options, kiosk, [])
    async with api, sim:
        await d.setup()
        visit = await d.visit("cancel")
        await _settle(d)

    assert visit is not None
    reservation, order = await _reservation(world, visit.reservation_id)
    assert reservation.status is ReservationStatus.CANCELLED
    assert order is not None and order.payment_status is PaymentStatus.VOIDED
    assert kiosk.typed == []


async def test_a_customer_who_never_comes_leaves_a_paid_hold_to_expire(
    setup: tuple[World, Options, FakeKiosk],
) -> None:
    world, options, kiosk = setup
    machine, _ = await _online_machine(world, {"A1": 5})
    _register(kiosk, machine)
    d, api, sim = _demo(options, kiosk, [])
    async with api, sim:
        await d.setup()
        visit = await d.visit("abandon")
        await _settle(d)

    assert visit is not None
    reservation, _ = await _reservation(world, visit.reservation_id)
    assert reservation.status is ReservationStatus.PAID
    assert kiosk.typed == []


async def test_shoppers_are_created_once_and_reused(
    setup: tuple[World, Options, FakeKiosk],
) -> None:
    world, options, kiosk = setup
    d, api, sim = _demo(options, kiosk, [])
    async with api, sim:
        await d.setup()
        await d.setup()  # a second run of the script
    async with world.sessions() as session:
        emails = list(await session.scalars(
            select(User.email).where(User.email.like(f"shopper-%@{options.shopper_domain}"))
        ))  # fmt: skip
    assert sorted(emails) == [f"shopper-{i}@{options.shopper_domain}" for i in (1, 2, 3)]


async def test_a_shopper_holds_one_reservation_at_a_time(
    setup: tuple[World, Options, FakeKiosk],
) -> None:
    world, options, kiosk = setup
    options.shoppers = 1
    machine, _ = await _online_machine(world, {"A1": 5})
    _register(kiosk, machine)
    d, api, sim = _demo(options, kiosk, [])
    async with api, sim:
        await d.setup()
        assert await d.visit("abandon") is not None
        assert await d.visit("abandon") is None  # busy until that hold ends


async def test_offline_maintenance_and_faulty_machines_are_skipped(
    setup: tuple[World, Options, FakeKiosk],
) -> None:
    world, options, kiosk = setup
    offline, _, _ = await world.machine({"A1": 5})  # never seen
    jammed, _ = await _online_machine(world, {"A1": 5})
    paused, _ = await _online_machine(world, {"A1": 5})
    async with world.sessions() as session:
        await session.execute(
            update(Machine).where(Machine.id == paused.id).values(status="maintenance")
        )
        await session.commit()
    _register(kiosk, offline)
    _register(kiosk, jammed, jam="always")
    _register(kiosk, paused)
    d, api, sim = _demo(options, kiosk, [])
    async with api, sim:
        await d.setup()
        assert await d.visit("pickup") is None


async def test_the_operator_refills_slots_running_low(
    setup: tuple[World, Options, FakeKiosk],
) -> None:
    world, options, kiosk = setup
    machine, slots = await _online_machine(world, {"A1": 1, "A2": 8})
    lines: list[str] = []
    d, api, sim = _demo(options, kiosk, lines)
    async with api, sim:
        await d.setup()
        refilled = await d.restock_if_low()

    assert refilled == [f"{machine.name} A1"]
    async with world.sessions() as session:
        quantities = dict((await session.execute(
            select(MachineSlot.slot_code, MachineSlot.quantity)
            .where(MachineSlot.machine_id == machine.id)
        )).tuples().all())  # fmt: skip
        movement = await session.scalar(
            select(StockMovement).where(StockMovement.slot_id == slots["A1"].id,
                                        StockMovement.reason == StockMovementReason.RESTOCK)
        )  # fmt: skip
    assert quantities == {"A1": 10, "A2": 8}
    assert movement is not None and movement.delta == 9


def test_the_scenario_mix_follows_the_weights() -> None:
    d = Demo(httpx.AsyncClient(), httpx.AsyncClient(), Options(), rng=random.Random(7))  # noqa: S311
    picks = [d.choose_scenario() for _ in range(4000)]
    for name, weight in SCENARIOS:
        assert abs(picks.count(name) / len(picks) - weight) < 0.03


def test_it_refuses_to_run_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "environment", "production")
    assert demo.main([]) == 1


@pytest.mark.parametrize("argv", [["--shoppers", "0"], ["--shoppers", "11"], ["--every", "0"]])
def test_bad_options_are_refused(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        demo.parse(argv)
