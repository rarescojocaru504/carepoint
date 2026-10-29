"""The simulated machine against a fake gateway: the protocol duties of
docs/machine-protocol.md, above all the §6 dispensing rules."""

import asyncio
import base64
import json
from collections.abc import Callable
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from carepoint_sim.config import Faults, SimConfig
from carepoint_sim.machine import SimulatedMachine
from carepoint_sim.signing import signed_headers
from carepoint_sim.state import InFlight, MachineState, StateStore
from tests.conftest import SERIAL
from tests.fake_gateway import FakeGateway

Start = Callable[..., Any]
Gateway = tuple[FakeGateway, str]


def _dispense(command_id: str = "c-1", slot: str = "A1", quantity: int = 1) -> dict[str, Any]:
    return {"type": "dispense", "command_id": command_id, "reservation_id": "r-1",
            "slot": slot, "quantity": quantity}  # fmt: skip


async def _online(machine: SimulatedMachine, within: float = 5) -> None:
    async with asyncio.timeout(within):
        while not machine.online:  # noqa: ASYNC110 (polling a test probe)
            await asyncio.sleep(0.01)


def test_signature_matches_the_adr_010_known_answer_vector() -> None:
    key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    body = json.dumps({"firmware_version": "1.0.0", "uptime_seconds": 42},
                      separators=(",", ":")).encode()  # fmt: skip
    headers = signed_headers(
        key, "CP-TEST-0001", method="POST", path="/api/v1/machine/heartbeat", body=body,
        timestamp=1767225600, nonce="A" * 22,
    )  # fmt: skip
    assert headers["X-CarePoint-Signature"] == (
        "3rLGXdo8ZelsHHTse4YjpmEwH21fWUHkSdCbEjHI33PrIhvHbpnU1OuCuQdnHHY25lFAcRF8wsJS2ZObgMTsCw"
    )
    public = key.public_key().public_bytes_raw()
    assert base64.urlsafe_b64encode(public).rstrip(b"=").decode() == (
        "A6EHv_POEL4dcN0Y50vAmWfk1jCbpQ1fHdyGZBJVMbg"
    )


async def test_connects_and_reports_in_protocol_order(gateway: Gateway, start: Start) -> None:
    fake, _ = gateway
    machine = await start()
    await fake.wait_for("heartbeat")

    kinds = [m["type"] for m in fake.received]
    assert kinds[:4] == ["stock_report", "telemetry", "heartbeat", "telemetry"]
    # A fresh machine adopts the server's counts, for every slot it has.
    assert fake.received[0]["slots"] == [
        {"slot": "A1", "quantity": 5}, {"slot": "A2", "quantity": 3}, {"slot": "B1", "quantity": 0},
    ]  # fmt: skip
    heartbeat = fake.of_type("heartbeat")[0]
    assert heartbeat["firmware_version"] == "sim-1.0.0"
    assert 18 <= fake.received[1]["temperature_c"] <= 25
    assert machine.online and machine.machine_id == "m-1"
    assert len({m["id"] for m in fake.received}) == len(fake.received)  # unique ids


async def test_own_counts_beat_the_servers_after_the_first_run(
    gateway: Gateway, start: Start, sim_config: SimConfig
) -> None:
    fake, _ = gateway
    StateStore(sim_config.state_dir / f"{SERIAL}.json").save(MachineState(counts={"A1": 2}))
    await start()
    [report] = await fake.wait_for("stock_report")
    assert {"slot": "A1", "quantity": 2} in report["slots"]


async def test_dispense_moves_stock_and_reports_the_result(
    gateway: Gateway, start: Start, sim_config: SimConfig
) -> None:
    fake, _ = gateway
    machine = await start()
    await fake.wait_for("heartbeat")

    await fake.push(_dispense(quantity=2))
    [result] = await fake.wait_for("dispense_result")

    assert result | {"id": None} == {"type": "dispense_result", "id": None, "command_id": "c-1",
                                     "outcome": "dispensed", "units_dispensed": 2}  # fmt: skip
    assert machine.state.counts["A1"] == 3
    await asyncio.sleep(0.05)
    assert machine.state.outbox == []  # acknowledged
    stored = StateStore(sim_config.state_dir / f"{SERIAL}.json").load()
    assert stored.counts["A1"] == 3 and "c-1" in stored.executed


async def test_a_command_is_executed_at_most_once(gateway: Gateway, start: Start) -> None:
    fake, _ = gateway
    machine = await start()
    await fake.wait_for("heartbeat")

    await fake.push(_dispense())
    await fake.wait_for("dispense_result")
    await fake.push(_dispense())  # resent by the server, e.g. after a reconnect
    results = await fake.wait_for("dispense_result", count=2)

    assert machine.state.counts["A1"] == 4  # one unit, not two
    assert results[0] | {"id": None} == results[1] | {"id": None}


async def test_resend_is_answered_even_while_the_first_answer_awaits_its_ack(
    gateway: Gateway, start: Start
) -> None:
    """Regression: a resent command arriving before the first result was
    acknowledged used to be dropped, leaving the server without an answer."""
    fake, _ = gateway
    machine = await start()
    await fake.wait_for("heartbeat")

    def hold_back_result_acks(message: dict[str, Any]) -> dict[str, Any] | None:
        if message["type"] == "dispense_result":
            return None
        return {"type": "ack", "reply_to": message["id"]}

    fake.reply = hold_back_result_acks

    await fake.push(_dispense())
    await fake.wait_for("dispense_result")  # sent, not acknowledged
    await fake.push(_dispense())  # the server resends

    results = await fake.wait_for("dispense_result", count=2)
    assert results[0]["command_id"] == results[1]["command_id"] == "c-1"
    assert machine.state.counts["A1"] == 4  # still dispensed only once


async def test_unacknowledged_result_is_sent_first_after_reconnecting(
    gateway: Gateway, start: Start
) -> None:
    fake, _ = gateway
    machine = await start(drop_next_result=True)
    await fake.wait_for("heartbeat")
    await fake.push(_dispense())

    # The machine dispensed, then lost the connection before reporting.
    await fake.wait_for("heartbeat", count=2)
    assert fake.connections == 2
    second = fake.received[fake.received.index(fake.of_type("heartbeat")[0]) + 2 :]
    assert [m["type"] for m in second[:2]] == ["dispense_result", "stock_report"]
    assert second[0]["command_id"] == "c-1"
    assert {"slot": "A1", "quantity": 4} in second[1]["slots"]  # already includes the dispense
    assert machine.state.outbox == []


async def test_result_without_ack_survives_a_dropped_connection(
    gateway: Gateway, start: Start
) -> None:
    fake, _ = gateway
    machine = await start()
    await fake.wait_for("heartbeat")

    def no_ack_for_results(message: dict[str, Any]) -> dict[str, Any] | None:
        if message["type"] == "dispense_result":
            return None
        return {"type": "ack", "reply_to": message["id"]}

    fake.reply = no_ack_for_results
    await fake.push(_dispense())
    await fake.wait_for("dispense_result")
    assert machine.state.outbox == ["c-1"]
    fake.reply = lambda m: {"type": "ack", "reply_to": m["id"]}
    await fake.drop()

    await fake.wait_for("dispense_result", count=2)
    await fake.wait_for("stock_report", count=2)
    assert machine.state.outbox == []
    assert machine.state.counts["A1"] == 4


@pytest.mark.parametrize(
    "disguise",
    [lambda: TimeoutError(), lambda: ExceptionGroup("session", [TimeoutError()])],
    ids=["timeout", "exception-group"],
)
async def test_stopping_works_even_when_the_cancellation_comes_back_disguised(
    sim_config: SimConfig, monkeypatch: pytest.MonkeyPatch, disguise: Callable[[], Exception]
) -> None:
    """3.11's wait_for can turn a cancellation into TimeoutError, and a task
    group would wrap that in an ExceptionGroup: run() must still stop."""
    connected = asyncio.Event()

    async def session(_: SimulatedMachine, ws: Any) -> None:
        connected.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise disguise() from None

    monkeypatch.setattr(SimulatedMachine, "_session", session)
    task = asyncio.create_task(SimulatedMachine(sim_config.machines[0], sim_config).run())
    await asyncio.wait_for(connected.wait(), 5)
    task.cancel()
    done, _ = await asyncio.wait({task}, timeout=5)
    assert task in done and task.cancelled()


async def test_jam_reports_a_failure_and_an_error_event(gateway: Gateway, start: Start) -> None:
    fake, _ = gateway
    machine = await start(jam="next")
    await fake.wait_for("heartbeat")

    await fake.push(_dispense("c-1"))
    [failed] = await fake.wait_for("dispense_result")
    [event] = await fake.wait_for("error_event")
    assert (failed["outcome"], failed["units_dispensed"], failed["error_code"]) == (
        "failed",
        0,
        "jam",
    )
    assert (event["code"], event["slot"]) == ("jam", "A1")
    assert machine.state.counts["A1"] == 5

    await fake.push(_dispense("c-2"))  # "next" means only the next one
    results = await fake.wait_for("dispense_result", count=2)
    assert results[1]["outcome"] == "dispensed"


async def test_partial_dispense_reports_the_units_that_came_out(
    gateway: Gateway, start: Start
) -> None:
    fake, _ = gateway
    machine = await start(partial_next=True)
    await fake.wait_for("heartbeat")

    await fake.push(_dispense(quantity=3))
    [result] = await fake.wait_for("dispense_result")
    assert (result["outcome"], result["units_dispensed"]) == ("failed", 1)
    assert machine.state.counts["A1"] == 4


async def test_empty_slot_fails_instead_of_inventing_items(gateway: Gateway, start: Start) -> None:
    fake, _ = gateway
    await start()
    await fake.wait_for("heartbeat")

    await fake.push(_dispense(slot="B1"))
    [result] = await fake.wait_for("dispense_result")
    assert (result["outcome"], result["units_dispensed"]) == ("failed", 0)


async def test_dispense_interrupted_by_a_crash_is_reported_not_repeated(
    gateway: Gateway, start: Start, sim_config: SimConfig
) -> None:
    fake, _ = gateway
    StateStore(sim_config.state_dir / f"{SERIAL}.json").save(
        MachineState(counts={"A1": 4}, in_flight=InFlight("c-9", "A1", 3, units_done=1))
    )
    machine = await start()

    [result] = await fake.wait_for("dispense_result")
    assert result | {"id": None} == {"type": "dispense_result", "id": None, "command_id": "c-9",
                                     "outcome": "failed", "units_dispensed": 1,
                                     "error_code": "power_loss"}  # fmt: skip
    assert fake.received[0]["type"] == "dispense_result"  # before the stock report
    await fake.push(_dispense("c-9", quantity=3))  # the server resends it
    await fake.wait_for("dispense_result", count=2)
    assert machine.state.counts["A1"] == 4  # never restarted


async def test_unknown_command_reply_discards_the_result(gateway: Gateway, start: Start) -> None:
    fake, _ = gateway

    def unknown(message: dict[str, Any]) -> dict[str, Any] | None:
        if message["type"] == "dispense_result":
            return {"type": "error", "reply_to": message["id"], "code": "unknown_command",
                    "detail": "no such command"}  # fmt: skip
        return {"type": "ack", "reply_to": message["id"]}

    fake.reply = unknown
    machine = await start()
    await fake.wait_for("heartbeat")
    await fake.push(_dispense())
    await fake.wait_for("dispense_result")
    await asyncio.sleep(0.05)
    assert machine.state.outbox == []


async def test_offline_fault_stays_away_until_cleared(gateway: Gateway, start: Start) -> None:
    fake, _ = gateway
    machine = await start()
    await _online(machine)

    machine.set_faults(Faults(seconds_per_unit=0, offline=True))
    await asyncio.sleep(0.5)  # several reconnect intervals
    assert not machine.online and fake.connections == 1

    machine.set_faults(Faults(seconds_per_unit=0))
    await _online(machine)
    assert fake.connections == 2


async def test_refused_handshakes_are_retried_with_backoff(gateway: Gateway, start: Start) -> None:
    fake, _ = gateway
    fake.refuse_next = 2
    machine = await start()

    await _online(machine)
    assert fake.connections == 1 and fake.refuse_next == 0


async def test_server_commands_after_welcome_are_handled(gateway: Gateway, start: Start) -> None:
    """The server resends open commands straight after `welcome`."""
    fake, _ = gateway
    fake.after_welcome = [_dispense("c-7")]
    machine = await start()

    [result] = await fake.wait_for("dispense_result")
    assert result["command_id"] == "c-7"
    assert machine.state.counts["A1"] == 4


async def test_operator_restock_is_counted_once_and_confirmed(
    gateway: Gateway, start: Start
) -> None:
    fake, _ = gateway
    machine = await start()
    await fake.wait_for("heartbeat")
    command = {"type": "adjust_stock", "command_id": "a-1", "slot": "A1", "delta": 4,
               "reason": "restock"}  # fmt: skip

    await fake.push(command)
    reports = await fake.wait_for("stock_report", count=2)
    assert reports[-1]["slots"] == [{"slot": "A1", "quantity": 9}]
    await fake.push(command)  # delivered twice: counted once
    reports = await fake.wait_for("stock_report", count=3)
    assert reports[-1]["slots"] == [{"slot": "A1", "quantity": 9}]
    assert machine.state.counts["A1"] == 9

    await fake.push(command | {"command_id": "a-2", "delta": -20, "reason": "adjustment"})
    await fake.wait_for("stock_report", count=4)
    assert machine.state.counts["A1"] == 0  # never below zero
