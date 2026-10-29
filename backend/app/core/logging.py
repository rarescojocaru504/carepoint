"""Structured logs with request IDs (ADR-029).

Every log line, the app's and uvicorn's, goes through one handler on the root
logger. With LOG_FORMAT=json (the default) each line is a JSON object:

    {"ts": "...", "level": "INFO", "logger": "carepoint.access",
     "message": "GET /api/v1/me 200", "request_id": "3f2c...", "status": 200, ...}

`request_id` comes from a context variable that the request middleware sets
for each HTTP request and WebSocket connection, so anything logged while
handling it, in any service, carries the same id.
"""

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, Literal

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

HANDLER_NAME = "carepoint"

# LogRecord's own attributes; anything else on a record came from `extra=`.
_STANDARD = set(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime"}
_PLAIN = (str, int, float, bool, list, dict)


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = request_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in vars(record).items():
            # Plain values only: libraries attach objects (uvicorn its WebSocket
            # protocol) whose repr is noise in a log line.
            if key not in _STANDARD and isinstance(value, _PLAIN):
                entry[key] = value
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)  # e.g. a datetime inside a dict


class TextFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(name)s [%(request_id)s] %(message)s")


def configure_logging(
    fmt: Literal["json", "text"], level: str = "INFO", stream: Any = None
) -> None:
    """Install the one handler. Safe to call again: it replaces its own handler."""
    root = logging.getLogger()
    for existing in [h for h in root.handlers if h.get_name() == HANDLER_NAME]:
        root.removeHandler(existing)
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.set_name(HANDLER_NAME)
    handler.addFilter(RequestIdFilter())
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    root.addHandler(handler)
    root.setLevel(level)
    # Uvicorn's loggers keep their own handlers and don't propagate; route them
    # through ours instead. Its access log is replaced by the middleware's,
    # which knows the request id, route and duration.
    for name in ("uvicorn", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
    access = logging.getLogger("uvicorn.access")
    access.handlers.clear()
    access.propagate = False
    access.disabled = True
