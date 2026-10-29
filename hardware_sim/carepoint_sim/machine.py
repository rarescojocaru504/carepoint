"""One simulated vending machine speaking docs/machine-protocol.md.

Connection loop: signed handshake -> `welcome` -> stored results first, then a
stock report and telemetry -> heartbeats, while a reader task dispatches
replies and commands. Dispensing runs on its own task, because the mechanism
keeps going when the network drops: its result waits in the outbox until the
server acknowledges it, possibly after several reconnects.
"""

import asyncio
import contextlib
import itertools
import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import aiohttp

from carepoint_sim.config import Faults, MachineConfig, SimConfig
from carepoint_sim.signing import GATEWAY_PATH, load_key, signed_headers
from carepoint_sim.state import InFlight, StateStore

logger = logging.getLogger(__name__)

SUBPROTOCOL = "carepoint.v1"
REPLY_TIMEOUT = 10.0


class NotConnectedError(Exception):
    pass


class _Disconnected(Exception):
    pass


@dataclass
class Slot:
    slot: str
    product_sku: str | None
    product_name: str | None
    capacity: int


class SimulatedMachine:
    def __init__(self, config: MachineConfig, sim: SimConfig) -> None:
        self.serial = config.serial
        self.firmware_version = config.firmware_version
        self.faults: Faults = config.faults.model_copy()
        self._sim = sim
        self._store = StateStore(sim.state_dir / f"{self.serial}.json")
        self.state = self._store.load()
        self.machine_id: str | None = None
        self.slots: dict[str, Slot] = {}
        self.door_open = False
        self.temperature_c = round(random.uniform(20.0, 22.0), 1)  # noqa: S311 (not crypto)
        self._started = time.monotonic()
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._ids = itertools.count(1)
        self._send_lock = asyncio.Lock()
        self._mechanism = asyncio.Lock()  # one dispense at a time
        self._background: set[asyncio.Task[None]] = set()
        self._allowed_online = asyncio.Event()
        if not self.faults.offline:
            self._allowed_online.set()
        self._listeners: set[asyncio.Queue[dict[str, Any]]] = set()

    # --- observers (the kiosk) ---------------------------------------------------

    @property
    def online(self) -> bool:
        return self._ws is not None

    def snapshot(self) -> dict[str, Any]:
        in_flight = self.state.in_flight
        return {
            "serial": self.serial,
            "machine_id": self.machine_id,
            "online": self.online,
            "faults": self.faults.model_dump(),
            "dispensing": None
            if in_flight is None
            else {
                "slot": in_flight.slot,
                "quantity": in_flight.quantity,
                "units_done": in_flight.units_done,
            },  # fmt: skip
            "slots": [
                {**vars(slot), "quantity": self.state.counts.get(code, 0)}
                for code, slot in sorted(self.slots.items())
            ],
        }

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=100)
        self._listeners.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._listeners.discard(queue)

    def _publish(self, event: str, **data: Any) -> None:
        message = {"event": event, **data, "machine": self.snapshot()}
        for queue in list(self._listeners):
            with contextlib.suppress(asyncio.QueueFull):  # a stalled screen misses events
                queue.put_nowait(message)

    def _save(self) -> None:
        self._store.save(self.state)

    # --- connection --------------------------------------------------------------

    async def run(self) -> None:
        """Stay connected forever, reconnecting with exponential backoff."""
        delay = self._sim.reconnect_initial_seconds
        async with aiohttp.ClientSession() as http:
            while True:
                await self._allowed_online.wait()
                try:
                    headers = signed_headers(
                        load_key(self._sim.keys_dir / f"{self.serial}.pem"), self.serial
                    )
                    async with http.ws_connect(
                        self._sim.backend_url + GATEWAY_PATH,
                        headers=headers,
                        protocols=(SUBPROTOCOL,),
                        timeout=aiohttp.ClientWSTimeout(ws_close=5),
                    ) as ws:
                        delay = self._sim.reconnect_initial_seconds
                        await self._session(ws)
                except (aiohttp.ClientError, OSError, TimeoutError, ValueError) as exc:
                    logger.warning("%s: cannot connect: %s", self.serial, exc)
                except Exception:
                    logger.exception("%s: connection failed", self.serial)
                finally:
                    self._disconnected()
                if not self._allowed_online.is_set():
                    continue  # offline on purpose: wait for the fault to clear
                jitter = random.uniform(0, delay / 2)  # noqa: S311
                await asyncio.sleep(delay + jitter)
                delay = min(delay * 2, self._sim.reconnect_max_seconds)

    async def _session(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        welcome = await ws.receive_json(timeout=REPLY_TIMEOUT)
        if welcome.get("type") != "welcome":
            raise ValueError(f"expected welcome, got {welcome.get('type')}")
        self._apply_welcome(welcome)
        self._ws = ws
        logger.info("%s: connected as %s", self.serial, self.machine_id)
        self._publish("online")
        try:
            async with asyncio.TaskGroup() as group:
                group.create_task(self._read(ws))
                group.create_task(self._lifecycle(welcome["heartbeat_interval_seconds"]))
        except* (_Disconnected, NotConnectedError):
            pass

    def _apply_welcome(self, welcome: dict[str, Any]) -> None:
        self.machine_id = welcome["machine_id"]
        self.slots = {
            s["slot"]: Slot(s["slot"], s["product_sku"], s["product_name"], s["capacity"])
            for s in welcome["slots"]
        }
        # A fresh machine adopts the server's counts once; afterwards its own
        # (persisted) counts are the truth it reports.
        for entry in welcome["slots"]:
            self.state.counts.setdefault(entry["slot"], entry["quantity"])
        for code in set(self.state.counts) - set(self.slots):
            del self.state.counts[code]
        self._save()

    def _disconnected(self) -> None:
        was_online = self._ws is not None
        self._ws = None
        for future in self._pending.values():
            if not future.done():
                future.set_exception(NotConnectedError())
        self._pending.clear()
        if was_online:
            logger.info("%s: disconnected", self.serial)
            self._publish("offline")

    async def _read(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        async for frame in ws:
            if frame.type is not aiohttp.WSMsgType.TEXT:
                break
            message: dict[str, Any] = frame.json()
            kind = message.get("type")
            if (reply_to := message.get("reply_to")) is not None:
                future = self._pending.pop(reply_to, None)
                if future is not None and not future.done():
                    future.set_result(message)
            elif kind == "dispense":
                self._spawn(self._on_dispense(message))
            else:
                logger.warning("%s: unexpected message %s", self.serial, kind)
        raise _Disconnected(f"closed with {ws.close_code}")

    async def _lifecycle(self, heartbeat_interval: int) -> None:
        # §6 rule 3: stored results first, so the stock report already includes them.
        await self._flush_outbox()
        await self.report_stock()
        await self._telemetry()
        while True:
            await self._request(
                {
                    "type": "heartbeat",
                    "firmware_version": self.firmware_version,
                    "uptime_seconds": int(time.monotonic() - self._started),
                }
            )
            await self._telemetry()
            await asyncio.sleep(heartbeat_interval)

    async def _request(self, message: dict[str, Any]) -> dict[str, Any]:
        """Send a message and wait for the reply to it."""
        ws = self._ws
        if ws is None:
            raise NotConnectedError()
        message_id = str(next(self._ids))
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[message_id] = future
        try:
            async with self._send_lock:
                await ws.send_json({**message, "id": message_id})
            reply = await asyncio.wait_for(future, REPLY_TIMEOUT)
        except (ConnectionError, aiohttp.ClientError) as exc:
            raise NotConnectedError() from exc
        finally:
            self._pending.pop(message_id, None)
        if reply.get("type") == "error":
            logger.warning("%s: %s rejected: %s", self.serial, message["type"], reply)
        return reply

    def _spawn(self, coroutine: Any) -> None:
        task = asyncio.create_task(coroutine)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _telemetry(self) -> None:
        drift = random.uniform(-0.2, 0.2)  # noqa: S311
        self.temperature_c = round(min(max(self.temperature_c + drift, 18.0), 25.0), 1)
        await self._request(
            {
                "type": "telemetry",
                "door": "open" if self.door_open else "closed",
                "temperature_c": self.temperature_c,
            }
        )

    async def report_stock(self) -> None:
        if self.state.counts:
            await self._request(
                {
                    "type": "stock_report",
                    "slots": [
                        {"slot": s, "quantity": q} for s, q in sorted(self.state.counts.items())
                    ],
                }
            )

    # --- the kiosk ---------------------------------------------------------------

    async def verify(self, credential: str) -> dict[str, Any]:
        """A customer entered a code: ask the server. When it is accepted the
        `dispense` command follows on its own."""
        if self.state.in_flight is not None:
            return {"ok": False, "refusal": "busy"}
        try:
            reply = await self._request({"type": "verify_code", "credential": credential})
        except (NotConnectedError, TimeoutError):
            return {"ok": False, "refusal": "offline"}
        if reply.get("type") == "error":
            return {"ok": False, "refusal": "invalid_code"}
        result = {key: value for key, value in reply.items() if key not in ("type", "reply_to")}
        self._publish("verified", result=result)
        return result

    async def restock(self) -> None:
        """An operator opens the door, fills every product slot, closes it."""
        self.door_open = True
        await self._quietly(self._telemetry)
        for code, slot in self.slots.items():
            if slot.product_sku is not None:
                self.state.counts[code] = slot.capacity
        self._save()
        self.door_open = False
        await self._quietly(self.report_stock)
        await self._quietly(self._telemetry)
        self._publish("restocked")

    def set_faults(self, faults: Faults) -> None:
        self.faults = faults
        if faults.offline:
            self._allowed_online.clear()
            self._spawn(self._hang_up())
        else:
            self._allowed_online.set()
        self._publish("faults")

    async def _hang_up(self) -> None:
        """Close the connection, but never wait on the server for long."""
        ws = self._ws
        if ws is None:
            return
        try:
            await asyncio.wait_for(ws.close(), timeout=5)
        except TimeoutError:
            logger.warning("%s: close handshake timed out", self.serial)

    async def _quietly(self, action: Callable[[], Any]) -> None:
        with contextlib.suppress(NotConnectedError, TimeoutError):
            await action()

    # --- dispensing (§6) ---------------------------------------------------------

    async def _on_dispense(self, command: dict[str, Any]) -> None:
        command_id: str = command["command_id"]
        in_flight = self.state.in_flight
        if in_flight is not None and in_flight.command_id == command_id:
            return  # resent while still dispensing: the result will follow
        if command_id not in self.state.executed:
            async with self._mechanism:
                if command_id not in self.state.executed:  # §6 rule 1: at most once
                    await self._dispense(command_id, command["slot"], command["quantity"])
                    if self.faults.drop_next_result:
                        self.faults.drop_next_result = False
                        await self._hang_up()  # the result stays in the outbox
                        return
        await self._deliver(command_id)

    async def _dispense(self, command_id: str, slot: str, quantity: int) -> None:
        self.state.in_flight = InFlight(command_id, slot, quantity)
        self._save()  # recorded *before* anything moves
        self._publish("dispensing", slot=slot, quantity=quantity)
        error: str | None = None
        for unit in range(quantity):
            await asyncio.sleep(self.faults.seconds_per_unit)
            if self.faults.jam != "off":
                error = "jam"
                if self.faults.jam == "next":
                    self.faults.jam = "off"
                break
            if self.state.counts.get(slot, 0) <= 0:
                error = "sensor_fault"  # nothing left to drop
                break
            self.state.counts[slot] -= 1
            self.state.in_flight.units_done += 1
            self._save()
            self._publish("unit_dispensed", slot=slot, unit=unit + 1, quantity=quantity)
            if self.faults.partial_next and unit + 1 < quantity:
                self.faults.partial_next = False
                error = "jam"
                break
        units = self.state.in_flight.units_done
        result = {
            "outcome": "failed" if error else "dispensed",
            "units_dispensed": units,
            "error_code": error,
        }
        self.state.in_flight = None
        self.state.record_result(command_id, result)
        self._save()  # §6 rule 2: stored before it is sent
        logger.info("%s: dispense %s from %s -> %s", self.serial, command_id, slot, result)
        self._publish("dispensed", slot=slot, result=result)
        if error is not None:
            await self._quietly(
                lambda: self._request({"type": "error_event", "code": error, "slot": slot})
            )

    async def _flush_outbox(self) -> None:
        for command_id in list(self.state.outbox):
            await self._deliver(command_id)

    async def _deliver(self, command_id: str) -> None:
        """Send a stored result; forget it once acknowledged (§6 rule 2).

        Every call sends, even if another send of the same result is still
        waiting for its ack: a resent `dispense` must always be answered, and
        the server ignores duplicates."""
        result = self.state.executed[command_id]
        payload = {"type": "dispense_result", "command_id": command_id, **result}
        if payload["error_code"] is None:
            del payload["error_code"]
        try:
            reply = await self._request(payload)
        except (NotConnectedError, TimeoutError):
            return  # stays in the outbox for the next connection
        if reply.get("type") == "ack" or reply.get("code") == "unknown_command":
            if command_id in self.state.outbox:
                self.state.outbox.remove(command_id)
                self._save()
