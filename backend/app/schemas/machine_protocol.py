"""Machine gateway protocol v1 (docs/machine-protocol.md, ADR-017).

Every WebSocket frame is one JSON object with a `type`. Messages from machines
are untrusted and validated strictly: unknown fields, wrong types (`"5"` for 5,
`true` for 1) and out-of-range values are rejected, never coerced. Messages
from the server are built by the server.

`docs/machine-protocol.schema.json` is generated from these models
(`python -m app.scripts.export_protocol_schema`), and a test keeps it in sync.
"""

import json
import re
import uuid
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from app.models.enums import MachineErrorCode
from app.schemas.common import InputModel
from app.schemas.machine_api import FirmwareVersion, UptimeSeconds

PROTOCOL_VERSION = 1
SUBPROTOCOL = "carepoint.v1"
MAX_MESSAGE_BYTES = 16 * 1024

_MESSAGE_ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"
MessageId = Annotated[str, Field(pattern=_MESSAGE_ID_PATTERN)]
SlotCode = Annotated[str, Field(pattern=r"^[A-Z][0-9]{1,2}$")]
SlotQuantity = Annotated[int, Field(strict=True, ge=0, le=1000)]
TemperatureC = Annotated[float, Field(strict=True, ge=-40, le=85, allow_inf_nan=False)]
# Free text from a machine ends up in logs and on the dashboard: no control characters.
EventText = Annotated[str, Field(pattern=r"^[^\x00-\x1f\x7f]{1,200}$")]
# What the customer typed or scanned, passed on verbatim (printable ASCII).
Credential = Annotated[str, Field(pattern=r"^[\x20-\x7e]{1,64}$")]
UnitCount = Annotated[int, Field(strict=True, ge=0, le=10)]


# --- machine -> server ----------------------------------------------------------


class _FromMachine(InputModel):
    id: MessageId = Field(description="Chosen by the machine; echoed as `reply_to`.")


class Heartbeat(_FromMachine):
    """Send every `heartbeat_interval_seconds` (from `welcome`)."""

    type: Literal["heartbeat"]
    firmware_version: FirmwareVersion
    uptime_seconds: UptimeSeconds


class Telemetry(_FromMachine):
    """Door and cabinet temperature; send on change and with every heartbeat."""

    type: Literal["telemetry"]
    door: Literal["open", "closed"]
    temperature_c: TemperatureC


class SlotCount(InputModel):
    slot: SlotCode
    quantity: SlotQuantity


class StockReport(_FromMachine):
    """Units physically in each listed slot, as counted by the machine now."""

    type: Literal["stock_report"]
    slots: Annotated[list[SlotCount], Field(min_length=1, max_length=64)]

    @field_validator("slots")
    @classmethod
    def _unique_slots(cls, slots: list[SlotCount]) -> list[SlotCount]:
        codes = [count.slot for count in slots]
        if len(codes) != len(set(codes)):
            raise ValueError("each slot may appear only once")
        return slots


class ErrorEvent(_FromMachine):
    """A fault the machine detected."""

    type: Literal["error_event"]
    code: MachineErrorCode
    slot: SlotCode | None = None
    message: EventText | None = None


class VerifyCode(_FromMachine):
    """A customer typed a pickup code or scanned its QR code."""

    type: Literal["verify_code"]
    credential: Credential = Field(description="6-digit code, or the QR payload CP1:<id>:<code>")


class DispenseResult(_FromMachine):
    """What happened to a `dispense` command. Send once per command, and again
    (same content) if it was not acknowledged before a disconnect."""

    type: Literal["dispense_result"]
    command_id: uuid.UUID
    outcome: Literal["dispensed", "failed"]
    units_dispensed: UnitCount
    error_code: MachineErrorCode | None = None

    @model_validator(mode="after")
    def _error_code_iff_failed(self) -> Self:
        if (self.outcome == "failed") != (self.error_code is not None):
            raise ValueError("error_code is required if and only if outcome is failed")
        return self


MachineMessage = Annotated[
    Heartbeat | Telemetry | StockReport | ErrorEvent | VerifyCode | DispenseResult,
    Field(discriminator="type"),
]
_machine_message: TypeAdapter[MachineMessage] = TypeAdapter(MachineMessage)


# --- server -> machine ----------------------------------------------------------


class ProtocolErrorCode(StrEnum):
    INVALID_MESSAGE = "invalid_message"
    UNKNOWN_SLOT = "unknown_slot"
    OUT_OF_RANGE = "out_of_range"
    SLOT_NOT_CONFIGURED = "slot_not_configured"
    UNKNOWN_COMMAND = "unknown_command"
    INTERNAL_ERROR = "internal_error"


class PickupRefusal(StrEnum):
    INVALID_CODE = "invalid_code"
    TOO_MANY_ATTEMPTS = "too_many_attempts"
    PICKUP_IN_PROGRESS = "pickup_in_progress"
    MACHINE_UNAVAILABLE = "machine_unavailable"


class _FromServer(BaseModel):
    # Fields with defaults are still always sent, so the schema lists them as required.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)


class Welcome(_FromServer):
    """First message after the handshake."""

    type: Literal["welcome"] = "welcome"
    protocol: int = PROTOCOL_VERSION
    machine_id: uuid.UUID
    serial_number: str
    server_time: datetime
    heartbeat_interval_seconds: int


class Ack(_FromServer):
    """The message `reply_to` was accepted and applied."""

    type: Literal["ack"] = "ack"
    reply_to: str


class Error(_FromServer):
    """The message `reply_to` was rejected and nothing of it was applied."""

    type: Literal["error"] = "error"
    reply_to: str | None = Field(description="null if the message had no readable id")
    code: ProtocolErrorCode
    detail: str


class VerifyResult(_FromServer):
    """Reply to `verify_code`. When `ok`, a `dispense` command follows at once."""

    type: Literal["verify_result"] = "verify_result"
    reply_to: str
    ok: bool
    refusal: PickupRefusal | None = None
    retry_after_seconds: int | None = Field(
        default=None, description="with too_many_attempts: when the keypad works again"
    )
    command_id: uuid.UUID | None = Field(default=None, description="the dispense that follows")


class Dispense(_FromServer):
    """Command: release `quantity` units from `slot`. Execute each `command_id`
    at most once; if it arrives again, resend its result instead."""

    type: Literal["dispense"] = "dispense"
    command_id: uuid.UUID
    reservation_id: uuid.UUID
    slot: str
    quantity: int


ServerMessage = Annotated[
    Welcome | Ack | Error | VerifyResult | Dispense, Field(discriminator="type")
]


# --- parsing --------------------------------------------------------------------


class InvalidMessageError(Exception):
    def __init__(self, reply_to: str | None, detail: str) -> None:
        super().__init__(detail)
        self.reply_to = reply_to
        self.detail = detail


def _readable_id(text: str) -> str | None:
    """The message's `id`, if one can be read, so even a rejection can be matched."""
    try:
        raw = json.loads(text)
    except ValueError:
        return None
    candidate = raw.get("id") if isinstance(raw, dict) else None
    if isinstance(candidate, str) and re.fullmatch(_MESSAGE_ID_PATTERN, candidate):
        return candidate
    return None


def _summarise(error: ValidationError) -> str:
    parts = []
    for item in error.errors(include_url=False, include_input=False)[:3]:
        location = ".".join(str(part) for part in item["loc"]) or "message"
        parts.append(f"{location}: {item['msg']}")
    return "; ".join(parts)[:300]


def parse_machine_message(text: str) -> MachineMessage:
    try:
        return _machine_message.validate_json(text)
    except ValidationError as exc:
        raise InvalidMessageError(_readable_id(text), _summarise(exc)) from None


def protocol_json_schema() -> dict[str, Any]:
    """Both directions as one JSON Schema document (draft 2020-12)."""
    machine = _machine_message.json_schema()
    server = TypeAdapter(ServerMessage).json_schema(mode="serialization")
    defs = {**machine.pop("$defs", {}), **server.pop("$defs", {})}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": f"CarePoint machine protocol v{PROTOCOL_VERSION} ({SUBPROTOCOL})",
        "$defs": {**defs, "MachineToServer": machine, "ServerToMachine": server},
    }
