import asyncio
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestServer
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

from carepoint_sim.config import Faults, MachineConfig, SimConfig
from carepoint_sim.machine import SimulatedMachine
from tests.fake_gateway import FakeGateway

SERIAL = "CP-SIM-0001"
PLANOGRAM: list[dict[str, Any]] = [
    {"slot": "A1", "product_sku": "PAD-REG-10", "product_name": "Pads", "capacity": 10,
     "quantity": 5},
    {"slot": "A2", "product_sku": "TOOTH-KIT", "product_name": "Toothbrush kit", "capacity": 8,
     "quantity": 3},
    {"slot": "B1", "product_sku": None, "product_name": None, "capacity": 6, "quantity": 0},
]  # fmt: skip


@pytest.fixture
def key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


@pytest.fixture
async def gateway(key: Ed25519PrivateKey) -> AsyncIterator[tuple[FakeGateway, str]]:
    fake = FakeGateway({SERIAL: key.public_key()}, [dict(slot) for slot in PLANOGRAM])
    server = TestServer(fake.app())
    await server.start_server()
    try:
        yield fake, f"ws://127.0.0.1:{server.port}"
    finally:
        await server.close()


@pytest.fixture
def sim_config(
    tmp_path: Path, key: Ed25519PrivateKey, gateway: tuple[FakeGateway, str]
) -> SimConfig:
    keys = tmp_path / "keys"
    keys.mkdir()
    pem = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    (keys / f"{SERIAL}.pem").write_bytes(pem)
    return SimConfig(
        backend_url=gateway[1], keys_dir=keys, state_dir=tmp_path / "state",
        reconnect_initial_seconds=0.05, reconnect_max_seconds=0.2,
        machines=[MachineConfig(serial=SERIAL, faults=Faults(seconds_per_unit=0))],
    )  # fmt: skip


@pytest.fixture
async def start(sim_config: SimConfig) -> AsyncIterator[Callable[..., Any]]:
    """Start a SimulatedMachine (several times, to simulate reboots)."""
    tasks: list[asyncio.Task[None]] = []

    async def _start(**faults: Any) -> SimulatedMachine:
        config = sim_config.machines[0].model_copy(
            update={"faults": Faults(seconds_per_unit=0, **faults)}
        )
        machine = SimulatedMachine(config, sim_config)
        tasks.append(asyncio.create_task(machine.run()))
        return machine

    yield _start
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
