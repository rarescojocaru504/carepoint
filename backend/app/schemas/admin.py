"""Operator dashboard API (ADR-021)."""

import uuid
from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import BaseModel, Field, model_validator

from app.models.enums import (
    MachineErrorCode,
    MachineEventKind,
    MachineStatus,
    PaymentStatus,
    ReservationStatus,
)
from app.schemas.common import InputModel, ReadModel
from app.schemas.discovery import Location
from app.schemas.inventory import StockMovementRead


class ProductBrief(BaseModel):
    id: uuid.UUID
    sku: str
    name: str


class AdminSlot(BaseModel):
    id: uuid.UUID
    slot_code: str
    product: ProductBrief | None
    quantity: int = Field(description="physical units in the slot")
    capacity: int
    low_stock_threshold: int
    held: int = Field(description="units held by active reservations")
    available: int
    low_stock: bool
    sold_out: bool


class AdminMachine(BaseModel):
    id: uuid.UUID
    serial_number: str
    name: str
    address: str
    location: Location
    status: MachineStatus
    online: bool
    last_seen_at: datetime | None
    firmware_version: str | None
    door_open: bool | None
    temperature_c: float | None
    telemetry_at: datetime | None
    units: int
    low_stock_slots: int
    sold_out_slots: int
    active_reservations: int


class MachineEventRead(ReadModel):
    id: int
    kind: MachineEventKind
    error_code: MachineErrorCode | None
    slot_code: str | None
    message: str | None
    created_at: datetime


class AdminMachineDetail(AdminMachine):
    slots: list[AdminSlot]
    recent_events: list[MachineEventRead]


class MachineStatusUpdate(InputModel):
    status: MachineStatus


class StockMovementWithActor(StockMovementRead):
    actor_email: str | None


class StockMovementResult(StockMovementWithActor):
    machine_notified: bool = Field(
        description="whether the machine's live connection got the adjust_stock command"
    )


class AdminReservation(BaseModel):
    id: uuid.UUID
    status: ReservationStatus
    user_email: str
    machine_id: uuid.UUID
    machine_serial: str
    slot_code: str
    product_sku: str
    product_name: str
    quantity: int
    total_cents: int
    payment_status: PaymentStatus | None
    created_at: datetime
    expires_at: datetime


ReservationFilter = Literal["active", "held", "paid", "dispensed", "expired", "cancelled"]


class KpiSummary(BaseModel):
    generated_at: datetime
    reservations_24h: int
    reservations_7d: int
    pickup_rate_7d: float | None = Field(
        description="dispensed / finished reservations created in the last 7 days"
    )
    expired_7d: int
    avg_hold_to_pickup_seconds_7d: float | None
    stock_outs: int = Field(description="assigned slots with no units, in active machines")
    low_stock_slots: int
    machines_online: int
    machines_total: int
    uptime_24h: float | None = Field(
        description="average share of the last 24 h (or since first connected, if later) "
        "that machines were connected; null before any machine has connected"
    )


_Note = Annotated[str, Field(min_length=1, max_length=500)]


class OperatorStockChange(InputModel):
    """A restock (units put in) or an adjustment (a counted correction, with a
    reason). Written to the ledger and sent to the machine as adjust_stock."""

    slot_id: uuid.UUID
    reason: Literal["restock", "adjustment"]
    delta: Annotated[int, Field(strict=True, ge=-100, le=100)]
    note: _Note | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.delta == 0:
            raise ValueError("delta must be non-zero")
        if self.reason == "restock" and self.delta < 0:
            raise ValueError("a restock adds units; use an adjustment to remove them")
        if self.reason == "adjustment" and self.note is None:
            raise ValueError("an adjustment needs a note saying why")
        return self
