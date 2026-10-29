"""A scriptable stand-in for the backend's machine gateway: checks the signed
handshake, sends `welcome`, records everything the machine sends and answers
it the way the real server does (overridable per test)."""

import asyncio
import base64
import json
import time
from collections.abc import Callable
from typing import Any

from aiohttp import web
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from carepoint_sim.signing import GATEWAY_PATH, canonical

Reply = Callable[[dict[str, Any]], dict[str, Any] | None]


def default_reply(message: dict[str, Any]) -> dict[str, Any] | None:
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


class FakeGateway:
    def __init__(self, keys: dict[str, Ed25519PublicKey], planogram: list[dict[str, Any]]) -> None:
        self.keys = keys
        self.planogram = planogram
        self.received: list[dict[str, Any]] = []
        self.connections = 0
        self.refuse_next = 0
        self.after_welcome: list[dict[str, Any]] = []
        self.reply: Reply = default_reply
        self.ws: web.WebSocketResponse | None = None
        self._request: web.Request | None = None

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get(GATEWAY_PATH, self._handle)
        return app

    def _authentic(self, request: web.Request) -> bool:
        h = request.headers
        serial = h.get("X-CarePoint-Machine", "")
        key = self.keys.get(serial)
        if key is None or "carepoint.v1" not in h.get("Sec-WebSocket-Protocol", ""):
            return False
        if abs(time.time() - int(h["X-CarePoint-Timestamp"])) > 60:
            return False
        signature = h["X-CarePoint-Signature"]
        message = canonical(
            method="GET",
            path=GATEWAY_PATH,
            serial=serial,
            timestamp=int(h["X-CarePoint-Timestamp"]),
            nonce=h["X-CarePoint-Nonce"],
            body=b"",
        )
        try:
            key.verify(base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4)), message)
        except InvalidSignature:
            return False
        return True

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        if self.refuse_next > 0 or not self._authentic(request):
            self.refuse_next = max(self.refuse_next - 1, 0)
            return web.json_response({"detail": "Invalid machine signature"}, status=401)
        ws = web.WebSocketResponse(protocols=("carepoint.v1",))
        await ws.prepare(request)
        self.connections += 1
        self.ws, self._request = ws, request
        await ws.send_json(
            {
                "type": "welcome",
                "protocol": 1,
                "machine_id": "m-1",
                "serial_number": request.headers["X-CarePoint-Machine"],
                "server_time": "2026-10-29T10:00:00Z",
                "heartbeat_interval_seconds": 3600,
                "slots": self.planogram,
            }
        )
        for message in self.after_welcome:
            await ws.send_json(message)
        async for frame in ws:
            message = json.loads(frame.data)
            self.received.append(message)
            reply = self.reply(message)
            if reply is not None:
                await ws.send_json(reply)
        return ws

    async def push(self, message: dict[str, Any]) -> None:
        assert self.ws is not None
        await self.ws.send_json(message)

    async def drop(self) -> None:
        """Lose the network: abort the TCP connection, no close handshake."""
        if self._request is not None and self._request.transport is not None:
            self._request.transport.close()

    def of_type(self, kind: str) -> list[dict[str, Any]]:
        return [m for m in self.received if m["type"] == kind]

    async def wait_for(self, kind: str, count: int = 1, within: float = 5) -> list[dict[str, Any]]:
        deadline = time.monotonic() + within
        while len(self.of_type(kind)) < count:
            assert time.monotonic() < deadline, f"no {count} x {kind}: {self.received}"
            await asyncio.sleep(0.01)
        return self.of_type(kind)
