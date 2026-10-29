"""Pickup at the machine over the gateway: verify_code -> dispense ->
dispense_result, including failures, retries and disconnects mid-dispense."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from redis.asyncio import Redis
from sqlalchemy import select, update

from app.core.config import get_settings
from app.models import (
    DispenseCommand,
    DispenseCommandStatus,
    Machine,
    MachineSlot,
    MachineStatus,
    Order,
    PaymentStatus,
    Reservation,
    ReservationStatus,
    StockMovement,
    StockMovementReason,
    User,
)
from app.schemas.reservation import ReservationCreate
from app.services import hold_store
from app.services.checkout import checkout
from app.services.payment_settlement import settle_pending
from app.services.payments import MockPaymentProvider
from app.services.reservation_expiry import expire_due_reservations
from app.services.reservations import (
    ReservationBeingDispensedError,
    cancel_reservation,
    create_reservation,
)
from tests.fake_machine import FakeMachine, MakeMachine, eventually
from tests.world import World

pytestmark = pytest.mark.integration


@dataclass
class Paid:
    user: User
    reservation_id: uuid.UUID
    code: str
    qr: str


@pytest.fixture
def pay(world: World, redis: Redis, provider: MockPaymentProvider) -> Any:
    """Reserve and pay through the real services; returns the pickup credential."""

    async def _pay(slot: MachineSlot, quantity: int = 1) -> Paid:
        [user] = await world.users(1)
        async with world.sessions() as session:
            view, _ = await create_reservation(
                session, redis, user, ReservationCreate(slot_id=slot.id, quantity=quantity)
            )
            result = await checkout(
                session, redis, provider, user, view.reservation.id, "pm_card_ok"
            )
        assert result.pickup is not None
        return Paid(user, result.reservation.id, result.pickup.code, result.pickup.qr_payload)

    return _pay


async def _reservation(world: World, reservation_id: uuid.UUID) -> Reservation:
    async with world.sessions() as session:
        reservation = await session.get(Reservation, reservation_id)
        assert reservation is not None
        return reservation


async def _order(world: World, reservation_id: uuid.UUID) -> Order:
    async with world.sessions() as session:
        order = await session.scalar(select(Order).where(Order.reservation_id == reservation_id))
        assert order is not None
        return order


async def _commands(world: World, reservation_id: uuid.UUID) -> list[DispenseCommand]:
    async with world.sessions() as session:
        return list(
            await session.scalars(
                select(DispenseCommand)
                .where(DispenseCommand.reservation_id == reservation_id)
                .order_by(DispenseCommand.created_at)
            )
        )


async def _ledger(world: World, slot_id: uuid.UUID) -> list[StockMovement]:
    async with world.sessions() as session:
        return list(
            await session.scalars(
                select(StockMovement)
                .where(
                    StockMovement.slot_id == slot_id,
                    StockMovement.reason != StockMovementReason.INITIAL,
                )
                .order_by(StockMovement.id)
            )
        )


async def _quantity(world: World, slot_id: uuid.UUID) -> int:
    async with world.sessions() as session:
        quantity = await session.scalar(
            select(MachineSlot.quantity).where(MachineSlot.id == slot_id)
        )
        assert quantity is not None
        return quantity


async def _start_pickup(fake: FakeMachine, credential: str) -> dict[str, Any]:
    """verify_code, expecting acceptance; returns the `dispense` that follows."""
    result = await fake.request("verify_code", credential=credential)
    assert result["ok"] is True, result
    dispense = await fake.recv()
    assert dispense["type"] == "dispense"
    assert dispense["command_id"] == result["command_id"]
    return dispense


async def _dispensed(fake: FakeMachine, dispense: dict[str, Any]) -> dict[str, Any]:
    return await fake.request(
        "dispense_result",
        command_id=dispense["command_id"],
        outcome="dispensed",
        units_dispensed=dispense["quantity"],
    )


# --- the happy path -------------------------------------------------------------


async def test_code_at_the_machine_dispenses_and_captures(
    make_machine: MakeMachine,
    world: World,
    redis: Redis,
    provider: MockPaymentProvider,
    pay: Any,
) -> None:
    fake, _, slots = await make_machine({"A1": 5})
    slot = slots["A1"]
    paid = await pay(slot)
    await fake.connect()

    dispense = await _start_pickup(fake, paid.code)
    assert dispense == {
        "type": "dispense",
        "command_id": dispense["command_id"],
        "reservation_id": str(paid.reservation_id),
        "slot": "A1",
        "quantity": 1,
    }
    [command] = await _commands(world, paid.reservation_id)
    assert (str(command.id), command.status) == (dispense["command_id"], "open")

    assert (await _dispensed(fake, dispense))["type"] == "ack"

    assert (await _reservation(world, paid.reservation_id)).status is ReservationStatus.DISPENSED
    order = await _order(world, paid.reservation_id)
    assert order.payment_status is PaymentStatus.CAPTURED
    assert order.payment_reference in provider.captured
    [command] = await _commands(world, paid.reservation_id)
    assert (command.status, command.units_dispensed) == (DispenseCommandStatus.SUCCEEDED, 1)
    assert command.completed_at is not None
    assert await _quantity(world, slot.id) == 4
    [movement] = await _ledger(world, slot.id)
    assert (movement.delta, movement.reason) == (-1, StockMovementReason.DISPENSE)
    snapshot = await hold_store.snapshot(redis, slot.id)
    assert (snapshot.physical, snapshot.held) == (4, 0)

    # The code is dead now.
    again = await fake.request("verify_code", credential=paid.code)
    assert (again["ok"], again["refusal"]) == (False, "invalid_code")


async def test_qr_payload_works_like_the_code(make_machine: MakeMachine, pay: Any) -> None:
    fake, _, slots = await make_machine()
    paid = await pay(slots["A1"])
    await fake.connect()

    dispense = await _start_pickup(fake, paid.qr)
    assert dispense["reservation_id"] == str(paid.reservation_id)


async def test_hold_is_extended_so_expiry_cannot_overtake_the_dispense(
    make_machine: MakeMachine,
    world: World,
    redis: Redis,
    pay: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "reservation_hold_seconds", 30)
    fake, _, slots = await make_machine()
    slot = slots["A1"]
    paid = await pay(slot)
    await fake.connect()

    before = datetime.now(UTC)
    await _start_pickup(fake, paid.code)

    minimum = before + timedelta(seconds=get_settings().dispense_timeout_seconds)
    assert (await _reservation(world, paid.reservation_id)).expires_at >= minimum
    hold = (await hold_store.read_state(redis, slot.id)).holds[paid.reservation_id]
    assert hold.expires_at >= minimum - timedelta(milliseconds=1)


# --- refusals -------------------------------------------------------------------


async def test_wrong_code_is_refused_without_counting_as_invalid(
    make_machine: MakeMachine, pay: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "machine_ws_max_invalid_messages", 1)
    fake, _, slots = await make_machine()
    paid = await pay(slots["A1"])
    await fake.connect()

    wrong = "000000" if paid.code != "000000" else "111111"
    for credential in (wrong, "not a code"):
        result = await fake.request("verify_code", credential=credential)
        assert result == {
            "type": "verify_result",
            "reply_to": result["reply_to"],
            "ok": False,
            "refusal": "invalid_code",
            "retry_after_seconds": None,
            "command_id": None,
        }
    # Still connected: the customer's typos are not the machine's fault.
    await _start_pickup(fake, paid.code)


async def test_guessing_locks_the_keypad(
    make_machine: MakeMachine, pay: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "rate_limit_pickup_failures_machine", "2/60")
    fake, _, slots = await make_machine()
    paid = await pay(slots["A1"])
    await fake.connect()

    wrong = "000000" if paid.code != "000000" else "111111"
    for _ in range(2):
        await fake.request("verify_code", credential=wrong)
    locked = await fake.request("verify_code", credential=paid.code)  # even the right one

    assert (locked["ok"], locked["refusal"]) == (False, "too_many_attempts")
    assert 0 < locked["retry_after_seconds"] <= 60


async def test_code_only_works_at_its_own_machine(make_machine: MakeMachine, pay: Any) -> None:
    _, _, slots = await make_machine()
    elsewhere, _, _ = await make_machine()
    paid = await pay(slots["A1"])
    await elsewhere.connect()

    result = await elsewhere.request("verify_code", credential=paid.code)
    assert result["refusal"] == "invalid_code"


async def test_machine_in_maintenance_does_not_dispense(
    make_machine: MakeMachine, world: World, pay: Any
) -> None:
    fake, machine, slots = await make_machine()
    paid = await pay(slots["A1"])
    async with world.sessions() as session:
        await session.execute(
            update(Machine).where(Machine.id == machine.id).values(status=MachineStatus.MAINTENANCE)
        )
        await session.commit()
    await fake.connect()

    result = await fake.request("verify_code", credential=paid.code)
    assert result["refusal"] == "machine_unavailable"


async def test_no_second_dispense_or_cancel_while_dispensing(
    make_machine: MakeMachine,
    world: World,
    redis: Redis,
    provider: MockPaymentProvider,
    pay: Any,
) -> None:
    fake, _, slots = await make_machine()
    paid = await pay(slots["A1"])
    await fake.connect()
    await _start_pickup(fake, paid.code)

    again = await fake.request("verify_code", credential=paid.code)
    assert again["refusal"] == "pickup_in_progress"
    async with world.sessions() as session:
        with pytest.raises(ReservationBeingDispensedError):
            await cancel_reservation(session, redis, provider, paid.user, paid.reservation_id)
    assert (await _reservation(world, paid.reservation_id)).status is ReservationStatus.PAID
    assert len(await _commands(world, paid.reservation_id)) == 1


# --- failed, partial and malformed results -------------------------------------


async def test_failed_dispense_keeps_the_order_and_allows_a_retry(
    make_machine: MakeMachine,
    world: World,
    provider: MockPaymentProvider,
    pay: Any,
) -> None:
    fake, _, slots = await make_machine()
    slot = slots["A1"]
    paid = await pay(slot)
    await fake.connect()
    first = await _start_pickup(fake, paid.code)

    reply = await fake.request(
        "dispense_result",
        command_id=first["command_id"],
        outcome="failed",
        units_dispensed=0,
        error_code="jam",
    )

    assert reply["type"] == "ack"
    assert (await _reservation(world, paid.reservation_id)).status is ReservationStatus.PAID
    assert (await _order(world, paid.reservation_id)).payment_status is PaymentStatus.AUTHORIZED
    assert await _ledger(world, slot.id) == []
    # The customer tries again with the same code, and this time it works.
    second = await _start_pickup(fake, paid.code)
    assert second["command_id"] != first["command_id"]
    await _dispensed(fake, second)
    assert (await _reservation(world, paid.reservation_id)).status is ReservationStatus.DISPENSED
    statuses = [(c.status, c.error_code) for c in await _commands(world, paid.reservation_id)]
    assert statuses == [
        (DispenseCommandStatus.FAILED, "jam"),
        (DispenseCommandStatus.SUCCEEDED, None),
    ]


async def test_partial_dispense_cancels_and_never_charges(
    make_machine: MakeMachine,
    world: World,
    redis: Redis,
    provider: MockPaymentProvider,
    pay: Any,
) -> None:
    fake, _, slots = await make_machine({"A1": 5})
    slot = slots["A1"]
    paid = await pay(slot, quantity=2)
    await fake.connect()
    dispense = await _start_pickup(fake, paid.code)

    reply = await fake.request(
        "dispense_result",
        command_id=dispense["command_id"],
        outcome="failed",
        units_dispensed=1,
        error_code="jam",
    )

    assert reply["type"] == "ack"
    assert (await _reservation(world, paid.reservation_id)).status is ReservationStatus.CANCELLED
    order = await _order(world, paid.reservation_id)
    assert order.payment_status is PaymentStatus.VOIDED
    assert order.payment_reference in provider.voided
    assert await _quantity(world, slot.id) == 4
    [movement] = await _ledger(world, slot.id)
    assert movement.delta == -1
    assert movement.note is not None and movement.note.startswith("partial dispense: 1 of 2")
    snapshot = await hold_store.snapshot(redis, slot.id)
    assert (snapshot.physical, snapshot.held) == (4, 0)


async def test_resent_result_is_applied_once(
    make_machine: MakeMachine, world: World, pay: Any
) -> None:
    fake, _, slots = await make_machine()
    slot = slots["A1"]
    paid = await pay(slot)
    await fake.connect()
    dispense = await _start_pickup(fake, paid.code)

    assert (await _dispensed(fake, dispense))["type"] == "ack"
    assert (await _dispensed(fake, dispense))["type"] == "ack"

    assert len(await _ledger(world, slot.id)) == 1
    assert await _quantity(world, slot.id) == 4


@pytest.mark.parametrize(
    ("outcome", "units", "error_code"),
    [("dispensed", 0, None), ("dispensed", 2, None), ("failed", 1, "jam")],
)
async def test_result_that_contradicts_the_command_is_rejected(
    make_machine: MakeMachine,
    world: World,
    pay: Any,
    outcome: str,
    units: int,
    error_code: str | None,
) -> None:
    fake, _, slots = await make_machine()
    paid = await pay(slots["A1"])  # one unit
    await fake.connect()
    dispense = await _start_pickup(fake, paid.code)

    reply = await fake.request(
        "dispense_result",
        command_id=dispense["command_id"],
        outcome=outcome,
        units_dispensed=units,
        error_code=error_code,
    )

    assert (reply["type"], reply["code"]) == ("error", "out_of_range")
    [command] = await _commands(world, paid.reservation_id)
    assert command.status is DispenseCommandStatus.OPEN


async def test_results_for_unknown_or_foreign_commands_are_rejected(
    make_machine: MakeMachine, world: World, pay: Any
) -> None:
    fake, _, slots = await make_machine()
    other, _, other_slots = await make_machine()
    paid = await pay(other_slots["A1"])
    await other.connect()
    theirs = await _start_pickup(other, paid.code)
    await fake.connect()

    for command_id in (str(uuid.uuid4()), theirs["command_id"]):
        reply = await fake.request(
            "dispense_result", command_id=command_id, outcome="dispensed", units_dispensed=1
        )
        assert reply["code"] == "unknown_command"
    assert (await _reservation(world, paid.reservation_id)).status is ReservationStatus.PAID


@pytest.mark.parametrize(
    "fields",
    [
        {"outcome": "failed", "units_dispensed": 0},  # failed needs an error_code
        {"outcome": "dispensed", "units_dispensed": 1, "error_code": "jam"},
        {"outcome": "dispensed", "units_dispensed": 11},
        {"outcome": "dispensed", "units_dispensed": "1"},
        {"outcome": "maybe", "units_dispensed": 1},
    ],
)
async def test_malformed_dispense_results_are_invalid(
    make_machine: MakeMachine, fields: dict[str, Any]
) -> None:
    fake, _, _ = await make_machine()
    await fake.connect()

    reply = await fake.request("dispense_result", command_id=str(uuid.uuid4()), **fields)
    assert reply["code"] == "invalid_message"


# --- disconnects ----------------------------------------------------------------


async def test_command_missed_during_a_disconnect_is_resent(
    make_machine: MakeMachine, world: World, pay: Any
) -> None:
    fake, _, slots = await make_machine()
    paid = await pay(slots["A1"])
    await fake.connect()
    await fake.send("verify_code", credential=paid.code)
    await fake.close()  # gone before reading the reply or the command

    async def issued() -> bool:
        return len(await _commands(world, paid.reservation_id)) == 1

    await eventually(issued)
    await fake.connect()
    resent = await fake.recv()  # straight after welcome

    [command] = await _commands(world, paid.reservation_id)
    assert (resent["type"], resent["command_id"]) == ("dispense", str(command.id))
    await _dispensed(fake, resent)
    assert (await _reservation(world, paid.reservation_id)).status is ReservationStatus.DISPENSED


async def test_disconnect_after_dispensing_before_the_result(
    make_machine: MakeMachine, world: World, provider: MockPaymentProvider, pay: Any
) -> None:
    """The machine dispensed, lost its connection, and kept the result in its
    outbox. On reconnect the server resends the command (it cannot know); the
    machine sends its stored result and answers the resend with it again."""
    fake, _, slots = await make_machine()
    slot = slots["A1"]
    paid = await pay(slot)
    await fake.connect()
    dispense = await _start_pickup(fake, paid.code)
    await fake.close()  # the item is out, the result never left the machine

    await fake.connect()
    result_id = await fake.send(
        "dispense_result",
        command_id=dispense["command_id"],
        outcome="dispensed",
        units_dispensed=1,
    )
    resent = await fake.recv()  # sent right after welcome, before our result arrived
    assert (resent["type"], resent["command_id"]) == ("dispense", dispense["command_id"])
    assert (await fake.recv()) == {"type": "ack", "reply_to": result_id}
    # The machine recognises the command id, does not dispense again, resends.
    assert (await _dispensed(fake, resent))["type"] == "ack"

    assert (await _reservation(world, paid.reservation_id)).status is ReservationStatus.DISPENSED
    assert (await _order(world, paid.reservation_id)).payment_status is PaymentStatus.CAPTURED
    assert len(await _ledger(world, slot.id)) == 1
    assert await _quantity(world, slot.id) == 4


async def test_machine_gone_past_the_deadline_is_never_charged(
    make_machine: MakeMachine,
    world: World,
    redis: Redis,
    provider: MockPaymentProvider,
    pay: Any,
) -> None:
    fake, _, slots = await make_machine()
    slot = slots["A1"]
    paid = await pay(slot)
    await fake.connect()
    dispense = await _start_pickup(fake, paid.code)
    await fake.close()

    # Time passes beyond the extended hold; the sweeper and settlement run.
    async with world.sessions() as session:
        await session.execute(
            update(Reservation)
            .where(Reservation.id == paid.reservation_id)
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await session.commit()
        assert await expire_due_reservations(session, redis) == [paid.reservation_id]
        await settle_pending(session, provider)
    [command] = await _commands(world, paid.reservation_id)
    assert command.status is DispenseCommandStatus.ABANDONED
    assert (await _order(world, paid.reservation_id)).payment_status is PaymentStatus.VOIDED

    # It comes back much later: nothing is resent, and its late result is
    # recorded as stock out, but nobody is charged.
    await fake.connect()
    reply = await _dispensed(fake, dispense)
    assert reply["type"] == "ack"

    assert (await _reservation(world, paid.reservation_id)).status is ReservationStatus.EXPIRED
    order = await _order(world, paid.reservation_id)
    assert order.payment_status is PaymentStatus.VOIDED
    assert order.payment_reference not in provider.captured
    [command] = await _commands(world, paid.reservation_id)
    assert command.status is DispenseCommandStatus.SUCCEEDED
    [movement] = await _ledger(world, slot.id)
    assert movement.delta == -1
    assert movement.note == "late dispense result; reservation was expired, not charged"
    assert await _quantity(world, slot.id) == 4


async def test_result_after_the_deadline_but_before_the_sweeper_is_late(
    make_machine: MakeMachine,
    world: World,
    redis: Redis,
    provider: MockPaymentProvider,
    pay: Any,
) -> None:
    """The deadline is what counts, not whether the sweeper has run yet."""
    fake, _, slots = await make_machine()
    slot = slots["A1"]
    paid = await pay(slot)
    await fake.connect()
    dispense = await _start_pickup(fake, paid.code)
    async with world.sessions() as session:
        await session.execute(
            update(Reservation)
            .where(Reservation.id == paid.reservation_id)
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await session.commit()

    assert (await _dispensed(fake, dispense))["type"] == "ack"

    order = await _order(world, paid.reservation_id)
    assert order.payment_reference not in provider.captured
    [movement] = await _ledger(world, slot.id)
    assert movement.note == "late dispense result; reservation was paid, not charged"
    # The sweeper then expires it and settlement voids the authorisation.
    async with world.sessions() as session:
        assert await expire_due_reservations(session, redis) == [paid.reservation_id]
        await settle_pending(session, provider)
    assert (await _order(world, paid.reservation_id)).payment_status is PaymentStatus.VOIDED
    [command] = await _commands(world, paid.reservation_id)
    assert command.status is DispenseCommandStatus.SUCCEEDED  # not abandoned: it reported


async def test_no_resend_once_the_deadline_has_passed(
    make_machine: MakeMachine, world: World, pay: Any
) -> None:
    """Even before the sweeper runs: nobody is waiting at the machine any more,
    so a resent command would hand an unpaid item to whoever walks by."""
    fake, _, slots = await make_machine()
    paid = await pay(slots["A1"])
    await fake.connect()
    await _start_pickup(fake, paid.code)
    await fake.close()
    async with world.sessions() as session:
        await session.execute(
            update(Reservation)
            .where(Reservation.id == paid.reservation_id)
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await session.commit()

    await fake.connect()
    # The first message after welcome is the reply to this, not a dispense.
    reply = await fake.request("heartbeat", firmware_version="1", uptime_seconds=1)
    assert reply["type"] == "ack"


async def test_stock_report_leaves_a_slot_mid_dispense_alone(
    make_machine: MakeMachine, world: World, pay: Any
) -> None:
    fake, _, slots = await make_machine({"A1": 5})
    slot = slots["A1"]
    paid = await pay(slot)
    await fake.connect()
    dispense = await _start_pickup(fake, paid.code)

    # The unit is already out; the result has not been sent yet.
    await fake.request("stock_report", slots=[{"slot": "A1", "quantity": 4}])
    assert await _quantity(world, slot.id) == 5  # not "corrected" ahead of the result

    await _dispensed(fake, dispense)
    await fake.request("stock_report", slots=[{"slot": "A1", "quantity": 4}])
    assert await _quantity(world, slot.id) == 4
    assert [m.reason for m in await _ledger(world, slot.id)] == [StockMovementReason.DISPENSE]
