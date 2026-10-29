"""A scriptable machine for gateway tests: a real WebSocket client that signs its
handshake exactly as device firmware must (docs/machine-protocol.md)."""

import asyncio
import itertools
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed
from websockets.typing import Subprotocol

from app.models import Machine, MachineSlot
from app.schemas.machine_protocol import SUBPROTOCOL
from app.services.machine_auth import sign_request

GATEWAY_PATH = "/api/v1/ws/machines"
TIMEOUT = 5


class FakeMachine:
    def __init__(self, base_url: str, serial_number: str, key: Ed25519PrivateKey) -> None:
        self.base_url = base_url
        self.serial_number = serial_number
        self.key = key
        self._ids = itertools.count(1)
        self._ws: ClientConnection | None = None

    def signed_headers(self, **overrides: Any) -> dict[str, str]:
        params: dict[str, Any] = {
            "serial_number": self.serial_number,
            "method": "GET",
            "path_and_query": GATEWAY_PATH,
        } | overrides
        return sign_request(self.key, **params)

    async def connect(
        self,
        headers: dict[str, str] | None = None,
        subprotocols: list[str] | None = None,
    ) -> dict[str, Any]:
        """Open the connection and return the server's `welcome`."""
        self._ws = await connect(
            self.base_url + GATEWAY_PATH,
            additional_headers=self.signed_headers() if headers is None else headers,
            subprotocols=[Subprotocol(p) for p in (subprotocols or [SUBPROTOCOL])],
            open_timeout=TIMEOUT,
        )
        return await self.recv()

    @property
    def ws(self) -> ClientConnection:
        assert self._ws is not None, "not connected"
        return self._ws

    async def send(self, type_: str, **fields: Any) -> str:
        message_id = f"m{next(self._ids)}"
        await self.ws.send(json.dumps({"type": type_, "id": message_id, **fields}))
        return message_id

    async def recv(self) -> dict[str, Any]:
        raw = await asyncio.wait_for(self.ws.recv(), TIMEOUT)
        message: dict[str, Any] = json.loads(raw)
        return message

    async def request(self, type_: str, **fields: Any) -> dict[str, Any]:
        """Send a message and return the reply to it."""
        message_id = await self.send(type_, **fields)
        reply = await self.recv()
        assert reply.get("reply_to") == message_id, reply
        return reply

    async def closed(self) -> tuple[int, str]:
        """Wait until the server closes the connection; return (code, reason)."""
        try:
            while True:
                await asyncio.wait_for(self.ws.recv(), TIMEOUT)
        except ConnectionClosed as exc:
            assert exc.rcvd is not None, "connection dropped without a close frame"
            return exc.rcvd.code, exc.rcvd.reason

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()


# The `make_machine` fixture (conftest.py): slots -> (client, machine row, slots by code).
MakeMachine = Callable[..., Awaitable[tuple["FakeMachine", Machine, dict[str, MachineSlot]]]]


async def eventually(check: Callable[[], Awaitable[bool]], within: float = TIMEOUT) -> None:
    """Poll `check` until it holds; fail after `within` seconds."""
    deadline = time.monotonic() + within
    while not await check():
        assert time.monotonic() < deadline, "condition not reached in time"
        await asyncio.sleep(0.05)
