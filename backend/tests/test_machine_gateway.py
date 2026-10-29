"""Machine gateway over a real WebSocket: handshake, telemetry, stock
reconciliation and the protocol's defences against a misbehaving machine."""

import asyncio
import json
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from redis.asyncio import Redis
from sqlalchemy import select, update
from websockets.exceptions import InvalidStatus

from app.core.config import get_settings
from app.core.security import create_access_token
from app.models import (
    Machine,
    MachineEvent,
    MachineEventKind,
    MachineSlot,
    MachineStatus,
    StockMovement,
    StockMovementReason,
    User,
    UserRole,
)
from app.schemas.machine_protocol import (
    InvalidMessageError,
    parse_machine_message,
)
from app.scripts.export_protocol_schema import render
from app.services import hold_store, machine_telemetry
from app.services.hold_store import SlotState
from tests.fake_machine import FakeMachine, MakeMachine, eventually
from tests.world import World

pytestmark = pytest.mark.integration


async def _events(world: World, machine_id: uuid.UUID) -> list[MachineEvent]:
    async with world.sessions() as session:
        return list(
            await session.scalars(
                select(MachineEvent)
                .where(MachineEvent.machine_id == machine_id)
                .order_by(MachineEvent.id)
            )
        )


async def _machine(world: World, machine_id: uuid.UUID) -> Machine:
    async with world.sessions() as session:
        machine = await session.get(Machine, machine_id)
        assert machine is not None
        return machine


async def _slot(world: World, slot_id: uuid.UUID) -> MachineSlot:
    async with world.sessions() as session:
        slot = await session.get(MachineSlot, slot_id)
        assert slot is not None
        return slot


async def _corrections(world: World, slot_id: uuid.UUID) -> list[StockMovement]:
    async with world.sessions() as session:
        return list(
            await session.scalars(
                select(StockMovement)
                .where(
                    StockMovement.slot_id == slot_id,
                    StockMovement.reason == StockMovementReason.TELEMETRY_CORRECTION,
                )
                .order_by(StockMovement.id)
            )
        )


# --- handshake ------------------------------------------------------------------


async def test_signed_handshake_is_welcomed(make_machine: MakeMachine, world: World) -> None:
    fake, machine, _ = await make_machine({"A1": 5, "B2": None})

    welcome = await fake.connect()

    assert welcome["type"] == "welcome"
    assert welcome["protocol"] == 1
    assert welcome["machine_id"] == str(machine.id)
    assert welcome["serial_number"] == machine.serial_number
    assert (
        welcome["heartbeat_interval_seconds"] == get_settings().machine_heartbeat_interval_seconds
    )
    a1, b2 = welcome["slots"]
    assert a1["product_sku"].startswith("GW-")
    assert a1 | {"product_sku": None} == {
        "slot": "A1", "product_sku": None, "product_name": "Gateway item",
        "capacity": 10, "quantity": 5,
    }  # fmt: skip
    assert b2 == {"slot": "B2", "product_sku": None, "product_name": None, "capacity": 10,
                  "quantity": 0}  # fmt: skip
    assert fake.ws.subprotocol == "carepoint.v1"
    assert [e.kind for e in await _events(world, machine.id)] == [MachineEventKind.CONNECTED]
    assert (await _machine(world, machine.id)).last_seen_at is not None


@pytest.mark.parametrize(
    "tamper",
    [
        pytest.param(lambda fake: {}, id="no-signature"),
        pytest.param(
            lambda fake: fake.signed_headers(path_and_query="/api/v1/machine/heartbeat"),
            id="signed-for-another-path",
        ),
        pytest.param(lambda fake: fake.signed_headers(method="POST"), id="signed-for-post"),
        pytest.param(
            lambda fake: fake.signed_headers(timestamp=int(time.time()) - 3600), id="stale"
        ),
        pytest.param(
            lambda fake: FakeMachine(
                fake.base_url, fake.serial_number, Ed25519PrivateKey.generate()
            ).signed_headers(),
            id="someone-elses-key",
        ),
        pytest.param(
            lambda fake: fake.signed_headers(serial_number="CP-NOPE-0001"), id="unknown-machine"
        ),
    ],
)
async def test_bad_handshakes_are_refused_with_401(
    make_machine: MakeMachine, world: World, tamper: Callable[[FakeMachine], dict[str, str]]
) -> None:
    fake, machine, _ = await make_machine()

    with pytest.raises(InvalidStatus) as refused:
        await fake.connect(headers=tamper(fake))

    assert refused.value.response.status_code == 401
    assert await _events(world, machine.id) == []


async def test_replayed_handshake_is_refused(make_machine: MakeMachine) -> None:
    fake, _, _ = await make_machine()
    headers = fake.signed_headers()
    await fake.connect(headers=headers)

    eavesdropper = FakeMachine(fake.base_url, fake.serial_number, fake.key)
    with pytest.raises(InvalidStatus) as refused:
        await eavesdropper.connect(headers=headers)
    assert refused.value.response.status_code == 401


async def test_handshake_without_subprotocol_is_refused(make_machine: MakeMachine) -> None:
    fake, _, _ = await make_machine()
    with pytest.raises(InvalidStatus) as refused:
        await fake.connect(subprotocols=["chat"])
    assert refused.value.response.status_code == 400


async def test_decommissioned_machine_cannot_connect(
    make_machine: MakeMachine, world: World
) -> None:
    fake, machine, _ = await make_machine()
    async with world.sessions() as session:
        await session.execute(
            update(Machine)
            .where(Machine.id == machine.id)
            .values(status=MachineStatus.DECOMMISSIONED)
        )
        await session.commit()

    with pytest.raises(InvalidStatus) as refused:
        await fake.connect()
    assert refused.value.response.status_code == 401


# --- telemetry ------------------------------------------------------------------


async def test_heartbeat_updates_liveness_and_firmware(
    make_machine: MakeMachine, world: World
) -> None:
    fake, machine, _ = await make_machine()
    await fake.connect()
    connected_at = (await _machine(world, machine.id)).last_seen_at
    assert connected_at is not None

    reply = await fake.request("heartbeat", firmware_version="2.0.1", uptime_seconds=120)

    assert reply == {"type": "ack", "reply_to": reply["reply_to"]}
    stored = await _machine(world, machine.id)
    assert stored.firmware_version == "2.0.1"
    assert stored.last_seen_at is not None and stored.last_seen_at >= connected_at


async def test_telemetry_is_stored_and_door_changes_are_logged(
    make_machine: MakeMachine, world: World
) -> None:
    fake, machine, _ = await make_machine()
    await fake.connect()

    for door, temperature in (("closed", 21.04), ("open", 21.5), ("open", 22), ("closed", -3.25)):
        reply = await fake.request("telemetry", door=door, temperature_c=temperature)
        assert reply["type"] == "ack"

    stored = await _machine(world, machine.id)
    assert stored.door_open is False
    assert str(stored.temperature_c) == "-3.2"  # one decimal, as stored
    assert stored.telemetry_at is not None
    assert [e.kind for e in await _events(world, machine.id)] == [
        MachineEventKind.CONNECTED,
        MachineEventKind.DOOR_CLOSED,  # first reading: state was unknown
        MachineEventKind.DOOR_OPENED,
        MachineEventKind.DOOR_CLOSED,  # the repeated "open" logged nothing
    ]


async def test_error_events_are_logged(make_machine: MakeMachine, world: World) -> None:
    fake, machine, _ = await make_machine()
    await fake.connect()

    reply = await fake.request("error_event", code="jam", slot="A1", message="motor stalled")

    assert reply["type"] == "ack"
    error = (await _events(world, machine.id))[-1]
    assert error.kind is MachineEventKind.ERROR
    assert (error.error_code, error.slot_code, error.message) == ("jam", "A1", "motor stalled")


# --- stock reconciliation -------------------------------------------------------


async def test_stock_report_corrects_the_database_and_redis(
    make_machine: MakeMachine, world: World, redis: Redis
) -> None:
    fake, _, slots = await make_machine({"A1": 5, "A2": 4})
    a1, a2 = slots["A1"], slots["A2"]
    await hold_store.initialise(redis, a1.id, SlotState(physical_quantity=5, holds=[]))
    await fake.connect()

    reply = await fake.request(
        "stock_report", slots=[{"slot": "A1", "quantity": 3}, {"slot": "A2", "quantity": 4}]
    )

    assert reply["type"] == "ack"
    assert (await _slot(world, a1.id)).quantity == 3
    [correction] = await _corrections(world, a1.id)
    assert (correction.delta, correction.quantity_after) == (-2, 3)
    assert correction.note == "machine counted 3, database had 5"
    assert (await hold_store.snapshot(redis, a1.id)).physical == 3
    # A matching count changes nothing and writes nothing.
    assert (await _slot(world, a2.id)).quantity == 4
    assert await _corrections(world, a2.id) == []


@pytest.mark.parametrize(
    ("report", "code"),
    [
        pytest.param([{"slot": "A1", "quantity": 1}, {"slot": "Z9", "quantity": 1}],
                     "unknown_slot", id="unknown-slot"),
        pytest.param([{"slot": "A1", "quantity": 1}, {"slot": "A2", "quantity": 11}],
                     "out_of_range", id="over-capacity"),
        pytest.param([{"slot": "A1", "quantity": 1}, {"slot": "B1", "quantity": 2}],
                     "slot_not_configured", id="items-in-unassigned-slot"),
    ],
)  # fmt: skip
async def test_bad_stock_report_is_rejected_as_a_whole(
    make_machine: MakeMachine, world: World, report: list[dict[str, Any]], code: str
) -> None:
    fake, _, slots = await make_machine({"A1": 5, "A2": 5, "B1": None})
    await fake.connect()

    reply = await fake.request("stock_report", slots=report)

    assert reply["type"] == "error"
    assert reply["code"] == code
    assert (await _slot(world, slots["A1"].id)).quantity == 5  # the valid part was not applied
    assert await _corrections(world, slots["A1"].id) == []


async def test_machine_cannot_touch_another_machines_slots(
    make_machine: MakeMachine, world: World
) -> None:
    fake, _, _ = await make_machine({"A1": 5})
    _, _, theirs = await make_machine({"A1": 5, "C3": 5})
    await fake.connect()

    reply = await fake.request("stock_report", slots=[{"slot": "C3", "quantity": 0}])

    assert reply["code"] == "unknown_slot"
    assert (await _slot(world, theirs["C3"].id)).quantity == 5
    # Same slot code on both machines: only the sender's own slot changes.
    await fake.request("stock_report", slots=[{"slot": "A1", "quantity": 2}])
    assert (await _slot(world, theirs["A1"].id)).quantity == 5


# --- validation -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "reply_to"),
    [
        pytest.param("not json", None, id="not-json"),
        pytest.param("[1, 2]", None, id="not-an-object"),
        pytest.param('{"type": "heartbeat"}', None, id="no-id"),
        pytest.param('{"type": "launch", "id": "x1"}', "x1", id="unknown-type"),
        pytest.param('{"type": "heartbeat", "id": "x1", "firmware_version": "1",'
                     ' "uptime_seconds": 1, "admin": true}', "x1", id="extra-field"),
        pytest.param('{"type": "heartbeat", "id": "x1", "firmware_version": "1",'
                     ' "uptime_seconds": "60"}', "x1", id="number-as-string"),
        pytest.param('{"type": "heartbeat", "id": "x1", "firmware_version": "1",'
                     ' "uptime_seconds": true}', "x1", id="bool-as-number"),
        pytest.param('{"type": "heartbeat", "id": "x1", "firmware_version": "1",'
                     ' "uptime_seconds": 1.5}', "x1", id="float-as-int"),
        pytest.param('{"type": "heartbeat", "id": "x1", "firmware_version": "1",'
                     ' "uptime_seconds": -1}', "x1", id="negative-uptime"),
        pytest.param('{"type": "heartbeat", "id": "x1", "firmware_version": "1; rm -rf",'
                     ' "uptime_seconds": 1}', "x1", id="bad-firmware-string"),
        pytest.param('{"type": "telemetry", "id": "x1", "door": "open",'
                     ' "temperature_c": NaN}', "x1", id="nan"),
        pytest.param('{"type": "telemetry", "id": "x1", "door": "open",'
                     ' "temperature_c": 300}', "x1", id="temperature-out-of-range"),
        pytest.param('{"type": "telemetry", "id": "x1", "door": "ajar",'
                     ' "temperature_c": 20}', "x1", id="unknown-door-state"),
        pytest.param('{"type": "stock_report", "id": "x1", "slots": []}', "x1",
                     id="empty-stock-report"),
        pytest.param('{"type": "stock_report", "id": "x1", "slots": [{"slot": "A1",'
                     ' "quantity": 1}, {"slot": "A1", "quantity": 2}]}', "x1",
                     id="duplicate-slot"),
        pytest.param('{"type": "stock_report", "id": "x1", "slots": [{"slot": "a1",'
                     ' "quantity": 1}]}', "x1", id="bad-slot-code"),
        pytest.param('{"type": "stock_report", "id": "x1", "slots": [{"slot": "A1",'
                     ' "quantity": -1}]}', "x1", id="negative-count"),
        pytest.param('{"type": "error_event", "id": "x1", "code": "meltdown"}', "x1",
                     id="unknown-error-code"),
        pytest.param('{"type": "error_event", "id": "x1", "code": "jam",'
                     ' "message": "line\\nbreak"}', "x1", id="control-character"),
        pytest.param('{"type": "heartbeat", "id": "../../etc", "firmware_version": "1",'
                     ' "uptime_seconds": 1}', None, id="bad-id"),
    ],
)  # fmt: skip
def test_malformed_messages_are_rejected(raw: str, reply_to: str | None) -> None:
    with pytest.raises(InvalidMessageError) as rejected:
        parse_machine_message(raw)
    assert rejected.value.reply_to == reply_to
    assert rejected.value.detail


async def test_rejected_message_gets_an_error_and_the_connection_stays_up(
    make_machine: MakeMachine,
) -> None:
    fake, _, _ = await make_machine()
    await fake.connect()

    await fake.ws.send(json.dumps({"type": "heartbeat", "id": "x1", "uptime_seconds": "1"}))
    error = await fake.recv()
    assert error["type"] == "error"
    assert error["reply_to"] == "x1"
    assert error["code"] == "invalid_message"
    assert "firmware_version" in error["detail"]

    assert (await fake.request("heartbeat", firmware_version="1", uptime_seconds=1))["type"] == (
        "ack"
    )


async def test_server_side_failure_is_reported_as_retryable(
    make_machine: MakeMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake, _, _ = await make_machine()
    await fake.connect()

    async def broken(*_: Any, **__: Any) -> None:
        raise RuntimeError("database on fire")

    monkeypatch.setattr(machine_telemetry, "record_heartbeat", broken)
    monkeypatch.setattr(get_settings(), "machine_ws_max_invalid_messages", 1)

    reply = await fake.request("heartbeat", firmware_version="1", uptime_seconds=1)
    assert (reply["code"], reply["detail"]) == ("internal_error", "internal error, retry later")
    # Not the machine's fault, so it does not count towards the invalid limit.
    assert (await fake.request("telemetry", door="closed", temperature_c=20))["type"] == "ack"


# --- connection policy ----------------------------------------------------------


async def test_too_many_invalid_messages_close_the_connection(
    make_machine: MakeMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "machine_ws_max_invalid_messages", 3)
    fake, _, _ = await make_machine()
    await fake.connect()

    for _ in range(3):
        await fake.ws.send("garbage")
    assert await fake.closed() == (1008, "too many invalid messages")


async def test_message_flood_closes_the_connection(
    make_machine: MakeMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "machine_ws_rate_limit", "5/60")
    fake, _, _ = await make_machine()
    await fake.connect()

    for _ in range(6):
        await fake.send("heartbeat", firmware_version="1", uptime_seconds=1)
    assert await fake.closed() == (1008, "message rate exceeded")


async def test_binary_frames_close_the_connection(make_machine: MakeMachine) -> None:
    fake, _, _ = await make_machine()
    await fake.connect()

    await fake.ws.send(b'{"type": "heartbeat"}')
    assert await fake.closed() == (1003, "text frames only")


async def test_oversized_message_closes_the_connection(make_machine: MakeMachine) -> None:
    fake, _, _ = await make_machine()
    await fake.connect()

    await fake.ws.send(json.dumps({"type": "error_event", "id": "x", "message": "x" * 20_000}))
    code, _ = await fake.closed()
    assert code == 1009


async def test_newer_connection_supersedes_the_old_one(
    make_machine: MakeMachine, world: World
) -> None:
    old, machine, _ = await make_machine()
    await old.connect()
    new = FakeMachine(old.base_url, old.serial_number, old.key)
    await new.connect()

    await old.send("heartbeat", firmware_version="1", uptime_seconds=1)
    assert await old.closed() == (4000, "superseded by a newer connection")
    assert (await new.request("heartbeat", firmware_version="1", uptime_seconds=2))["type"] == (
        "ack"
    )
    await new.close()


async def test_silent_connection_is_closed(
    make_machine: MakeMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "machine_online_window_seconds", 1)
    fake, _, _ = await make_machine()
    await fake.connect()

    assert await fake.closed() == (4008, "no message within the online window")


async def test_decommissioning_cuts_off_a_connected_machine(
    make_machine: MakeMachine, world: World
) -> None:
    fake, machine, _ = await make_machine()
    await fake.connect()
    async with world.sessions() as session:
        await session.execute(
            update(Machine)
            .where(Machine.id == machine.id)
            .values(status=MachineStatus.DECOMMISSIONED)
        )
        await session.commit()

    await fake.send("heartbeat", firmware_version="1", uptime_seconds=1)
    assert await fake.closed() == (4003, "machine decommissioned")


async def test_disconnect_is_logged(make_machine: MakeMachine, world: World) -> None:
    fake, machine, _ = await make_machine()
    await fake.connect()
    await fake.close()

    async def logged() -> bool:
        kinds = [e.kind for e in await _events(world, machine.id)]
        return kinds == [MachineEventKind.CONNECTED, MachineEventKind.DISCONNECTED]

    await eventually(logged)


# --- the published spec ---------------------------------------------------------

SCHEMA_FILE = Path(__file__).resolve().parents[2] / "docs" / "machine-protocol.schema.json"


@pytest.mark.skipif(not SCHEMA_FILE.exists(), reason="docs/ is not in the Docker image")
def test_published_json_schema_matches_the_code() -> None:
    assert SCHEMA_FILE.read_text() == render(), (
        "docs/machine-protocol.schema.json is stale: run "
        "`python -m app.scripts.export_protocol_schema > ../docs/machine-protocol.schema.json`"
    )


# --- operators and the live connection (ADR-021) ---------------------------------


async def _operator_headers(world: World) -> dict[str, str]:
    operator = User(email=f"op-{uuid.uuid4().hex[:10]}@example.com", password_hash="!",
                    full_name="Operator", role=UserRole.OPERATOR)  # fmt: skip
    await world.add(operator)
    return {"Authorization": f"Bearer {create_access_token(operator.id, operator.role)[0]}"}


def _http(gateway_url: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=gateway_url.replace("ws://", "http://"), timeout=10)


async def test_operator_restock_reaches_the_connected_machine(
    make_machine: MakeMachine, world: World, gateway_url: str
) -> None:
    fake, _, slots = await make_machine({"A1": 2})
    await fake.connect()
    headers = await _operator_headers(world)

    async with _http(gateway_url) as http:
        response = await http.post(
            "/api/v1/admin/stock-movements",
            json={"slot_id": str(slots["A1"].id), "reason": "restock", "delta": 5},
            headers=headers,
        )
    assert response.status_code == 201, response.text
    assert response.json()["machine_notified"] is True

    command = await fake.recv()
    assert command | {"command_id": None} == {
        "type": "adjust_stock", "command_id": None, "slot": "A1", "delta": 5, "reason": "restock",
    }  # fmt: skip
    assert (await _slot(world, slots["A1"].id)).quantity == 7
    # The machine confirms with a stock report that matches: nothing to correct.
    await fake.request("stock_report", slots=[{"slot": "A1", "quantity": 7}])
    assert await _corrections(world, slots["A1"].id) == []


async def test_disconnect_shows_offline_at_once_and_reconnect_online(
    make_machine: MakeMachine, world: World, gateway_url: str
) -> None:
    fake, machine, _ = await make_machine()
    headers = await _operator_headers(world)

    async def online() -> bool:
        async with _http(gateway_url) as http:
            detail = await http.get(f"/api/v1/admin/machines/{machine.id}", headers=headers)
        return bool(detail.json()["online"])

    await fake.connect()
    assert await online()
    await fake.close()

    async def offline() -> bool:
        return not await online()

    await eventually(offline, within=3)  # not the 90 s online window
    await fake.connect()
    await eventually(online, within=3)


async def test_superseded_connection_closing_does_not_mark_offline(
    make_machine: MakeMachine, world: World
) -> None:
    old, machine, _ = await make_machine()
    await old.connect()
    new = FakeMachine(old.base_url, old.serial_number, old.key)
    await new.connect()

    await old.send("heartbeat", firmware_version="1", uptime_seconds=1)
    assert (await old.closed())[0] == 4000
    await asyncio.sleep(0.2)

    stored = await _machine(world, machine.id)
    assert stored.disconnected_at is None
    kinds = [e.kind for e in await _events(world, machine.id)]
    assert kinds == [MachineEventKind.CONNECTED, MachineEventKind.CONNECTED]
    await new.close()


async def test_machine_vanishing_mid_reply_is_an_ordinary_disconnect(
    make_machine: MakeMachine, world: World, caplog: pytest.LogCaptureFixture
) -> None:
    fake, machine, _ = await make_machine()
    await fake.connect()
    for _ in range(5):  # replies still on their way when the socket goes
        await fake.send("heartbeat", firmware_version="1", uptime_seconds=1)
    await fake.close()

    async def logged() -> bool:
        return MachineEventKind.DISCONNECTED in [e.kind for e in await _events(world, machine.id)]

    await eventually(logged)
    assert "Exception in ASGI application" not in caplog.text
