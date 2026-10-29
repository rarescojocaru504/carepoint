"""The local kiosk API: what the touchscreen calls."""

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from carepoint_sim.config import SimConfig
from carepoint_sim.kiosk_api import build_app
from carepoint_sim.machine import SimulatedMachine
from tests.conftest import SERIAL
from tests.fake_gateway import FakeGateway

Gateway = tuple[FakeGateway, str]


@pytest.fixture
async def kiosk(
    start: Callable[..., Any], sim_config: SimConfig, gateway: Gateway
) -> AsyncIterator[tuple[TestClient[Any, Any], SimulatedMachine]]:
    machine: SimulatedMachine = await start()
    await gateway[0].wait_for("heartbeat")
    client = TestClient(TestServer(build_app({SERIAL: machine}, sim_config)))
    await client.start_server()
    try:
        yield client, machine
    finally:
        await client.close()


def _accepting(command_id: str = "c-1") -> Callable[[dict[str, Any]], dict[str, Any] | None]:
    def reply(message: dict[str, Any]) -> dict[str, Any] | None:
        if message["type"] == "verify_code" and message["credential"] == "123456":
            return {
                "type": "verify_result",
                "reply_to": message["id"],
                "ok": True,
                "refusal": None,
                "retry_after_seconds": None,
                "command_id": command_id,
            }
        if message["type"] == "verify_code":
            return {
                "type": "verify_result",
                "reply_to": message["id"],
                "ok": False,
                "refusal": "invalid_code",
                "retry_after_seconds": None,
                "command_id": None,
            }
        return {"type": "ack", "reply_to": message["id"]}

    return reply  # fmt: skip


async def test_snapshot_lists_slots_with_local_counts(kiosk: Any) -> None:
    client, _ = kiosk
    [machine] = await (await client.get("/api/machines")).json()

    assert machine["serial"] == SERIAL and machine["online"] is True
    assert machine["slots"][0] == {
        "slot": "A1", "product_sku": "PAD-REG-10", "product_name": "Pads", "capacity": 10,
        "quantity": 5,
    }  # fmt: skip
    assert (await client.get("/api/machines/CP-NOPE-0001")).status == 404


async def test_entered_code_is_forwarded_and_the_dispense_follows(
    kiosk: Any, gateway: Gateway
) -> None:
    client, machine = kiosk
    fake, _ = gateway
    fake.reply = _accepting()
    events = await client.get(f"/api/machines/{SERIAL}/events")

    response = await client.post(f"/api/machines/{SERIAL}/verify", json={"credential": " 123456 "})
    assert (await response.json())["ok"] is True
    assert fake.of_type("verify_code")[-1]["credential"] == "123456"
    await fake.push({"type": "dispense", "command_id": "c-1", "reservation_id": "r-1",
                     "slot": "A1", "quantity": 1})  # fmt: skip
    await fake.wait_for("dispense_result")

    seen = []
    async with asyncio.timeout(5):
        while "dispensed" not in seen:
            line = (await events.content.readline()).decode().strip()
            if line.startswith("data: "):
                seen.append(json.loads(line[6:])["event"])
    assert seen[0] == "snapshot"
    assert seen.index("verified") < seen.index("dispensing") < seen.index("dispensed")
    assert machine.state.counts["A1"] == 4
    events.close()


async def test_wrong_code_and_bad_input(kiosk: Any, gateway: Gateway) -> None:
    client, _ = kiosk
    gateway[0].reply = _accepting()

    wrong = await client.post(f"/api/machines/{SERIAL}/verify", json={"credential": "000000"})
    assert (await wrong.json())["refusal"] == "invalid_code"
    for body in ({"credential": ""}, {"credential": "x" * 65}, {"credential": 123456}, ["x"]):
        assert (await client.post(f"/api/machines/{SERIAL}/verify", json=body)).status in (400, 422)


async def test_offline_machine_refuses_codes_locally(kiosk: Any) -> None:
    client, _ = kiosk
    response = await client.put(f"/api/machines/{SERIAL}/faults", json={"offline": True})
    assert (await response.json())["faults"]["offline"] is True
    await asyncio.sleep(0.1)

    result = await client.post(f"/api/machines/{SERIAL}/verify", json={"credential": "123456"})
    assert (await result.json()) == {"ok": False, "refusal": "offline"}


async def test_fault_injection_is_validated(kiosk: Any) -> None:
    client, machine = kiosk
    ok = await client.put(f"/api/machines/{SERIAL}/faults", json={"jam": "always"})
    assert (await ok.json())["faults"]["jam"] == "always"
    for body in ({"jam": "sometimes"}, {"seconds_per_unit": -1}, {"explode": True}):
        assert (await client.put(f"/api/machines/{SERIAL}/faults", json=body)).status == 422
    assert machine.faults.jam == "always"


async def test_restock_fills_product_slots_and_reports(kiosk: Any, gateway: Gateway) -> None:
    client, _ = kiosk
    fake, _ = gateway
    snapshot = await (await client.post(f"/api/machines/{SERIAL}/restock")).json()

    quantities = {s["slot"]: s["quantity"] for s in snapshot["slots"]}
    assert quantities == {"A1": 10, "A2": 8, "B1": 0}  # B1 has no product
    report = (await fake.wait_for("stock_report", count=2))[-1]
    assert {"slot": "A1", "quantity": 10} in report["slots"]
    doors = [m["door"] for m in fake.of_type("telemetry")]
    assert doors[-2:] == ["open", "closed"]
