"""The machines' local HTTP API, used by the kiosk screen (and by demos).

On real hardware this would listen on localhost only: the touchscreen and the
controller are the same box. It is unauthenticated for that reason; never
expose it to a network you do not control.

    GET  /api/machines                    all machines (snapshot)
    GET  /api/machines/{serial}           one machine
    GET  /api/machines/{serial}/events    server-sent events: snapshot, then changes
    POST /api/machines/{serial}/verify    {"credential": "..."}: a customer entered a code
    PUT  /api/machines/{serial}/faults    fault injection (see config.Faults)
    POST /api/machines/{serial}/restock   fill every product slot
"""

import asyncio
import json
import re
from typing import Any

from aiohttp import web
from pydantic import ValidationError

from carepoint_sim.config import Faults, SimConfig
from carepoint_sim.machine import SimulatedMachine

_CREDENTIAL = re.compile(r"^[\x20-\x7e]{1,64}$")
_KEEPALIVE_SECONDS = 15
MACHINES: web.AppKey[dict[str, SimulatedMachine]] = web.AppKey("machines")


def _machine(request: web.Request) -> SimulatedMachine:
    machine = request.app[MACHINES].get(request.match_info["serial"])
    if machine is None:
        raise web.HTTPNotFound(text=json.dumps({"detail": "unknown machine"}),
                               content_type="application/json")  # fmt: skip
    return machine


async def _body(request: web.Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text=json.dumps({"detail": "JSON object expected"}),
                                 content_type="application/json")  # fmt: skip
    return body


async def list_machines(request: web.Request) -> web.Response:
    return web.json_response([m.snapshot() for m in request.app[MACHINES].values()])


async def get_machine(request: web.Request) -> web.Response:
    return web.json_response(_machine(request).snapshot())


async def verify(request: web.Request) -> web.Response:
    machine = _machine(request)
    credential = (await _body(request)).get("credential")
    if not isinstance(credential, str) or not _CREDENTIAL.fullmatch(credential.strip()):
        return web.json_response({"detail": "credential must be 1-64 printable characters"},
                                 status=422)  # fmt: skip
    return web.json_response(await machine.verify(credential.strip()))


async def set_faults(request: web.Request) -> web.Response:
    machine = _machine(request)
    try:
        faults = Faults.model_validate(machine.faults.model_dump() | await _body(request))
    except ValidationError as exc:
        return web.json_response({"detail": exc.errors(include_url=False)}, status=422)
    machine.set_faults(faults)
    return web.json_response(machine.snapshot())


async def restock(request: web.Request) -> web.Response:
    machine = _machine(request)
    await machine.restock()
    return web.json_response(machine.snapshot())


async def events(request: web.Request) -> web.StreamResponse:
    machine = _machine(request)
    response = web.StreamResponse(
        headers={"Content-Type": "text/event-stream", "Cache-Control": "no-store"}
    )
    await response.prepare(request)
    queue = machine.subscribe()
    try:
        await _send(response, {"event": "snapshot", "machine": machine.snapshot()})
        while True:
            try:
                message = await asyncio.wait_for(queue.get(), _KEEPALIVE_SECONDS)
            except TimeoutError:
                await response.write(b": keep-alive\n\n")
                continue
            await _send(response, message)
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        machine.unsubscribe(queue)
    return response


async def _send(response: web.StreamResponse, message: dict[str, Any]) -> None:
    await response.write(f"data: {json.dumps(message)}\n\n".encode())


def build_app(machines: dict[str, SimulatedMachine], config: SimConfig) -> web.Application:
    app = web.Application()
    app[MACHINES] = machines
    app.add_routes(
        [
            web.get("/api/machines", list_machines),
            web.get("/api/machines/{serial}", get_machine),
            web.get("/api/machines/{serial}/events", events),
            web.post("/api/machines/{serial}/verify", verify),
            web.put("/api/machines/{serial}/faults", set_faults),
            web.post("/api/machines/{serial}/restock", restock),
        ]
    )
    if config.kiosk_dir is not None and (config.kiosk_dir / "index.html").exists():
        index = config.kiosk_dir / "index.html"

        async def spa(_: web.Request) -> web.FileResponse:
            return web.FileResponse(index)

        app.router.add_static("/assets", config.kiosk_dir / "assets")
        app.router.add_get("/", spa)
        app.router.add_get("/kiosk/{serial}", spa)
    return app
