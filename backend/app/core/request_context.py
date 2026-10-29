"""Request IDs, access logs and HTTP metrics for every request (ADR-029).

A plain ASGI middleware rather than Starlette's BaseHTTPMiddleware, which
buffers responses differently and doesn't suit event streams or WebSockets.
"""

import logging
import re
import time
import uuid

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.logging import request_id_var
from app.core.metrics import HTTP_DURATION, HTTP_REQUESTS

logger = logging.getLogger("carepoint.access")

HEADER = "X-Request-ID"
# A caller's id (a load balancer's, or the app's own) is kept only if it is
# short and plain, so it can't be used to forge or bloat log lines.
_VALID_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
# Probes hit these every few seconds; their access lines would drown the rest.
_QUIET_PATHS = {"/health", "/ready", "/metrics"}


def _route(scope: Scope) -> str:
    route = scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else "unmatched"


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        incoming = Headers(scope=scope).get(HEADER.lower())
        request_id = incoming if incoming and _VALID_ID.match(incoming) else uuid.uuid4().hex
        token = request_id_var.set(request_id)
        if scope["type"] == "websocket":
            try:
                await self.app(scope, receive, send)
            finally:
                request_id_var.reset(token)
            return

        status = 500
        started = time.perf_counter()

        async def send_with_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message).append(HEADER, request_id)
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            elapsed = time.perf_counter() - started
            method, path, route = scope["method"], scope["path"], _route(scope)
            HTTP_REQUESTS.labels(method, route, str(status)).inc()
            HTTP_DURATION.labels(method, route).observe(elapsed)
            logger.log(
                logging.DEBUG if path in _QUIET_PATHS else logging.INFO,
                "%s %s %s",
                method,
                path,
                status,
                extra={
                    "request_id": request_id,
                    "method": method,
                    "path": path,
                    "route": route,
                    "status": status,
                    "duration_ms": round(elapsed * 1000, 1),
                },
            )
            request_id_var.reset(token)
