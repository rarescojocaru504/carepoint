import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field

from app.models.enums import PaymentStatus, ReservationStatus
from app.schemas.common import InputModel, ReadModel

ReservationQty = Annotated[int, Field(strict=True, gt=0, le=10)]
IdempotencyKey = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{8,64}$")]


class ReservationCreate(InputModel):
    slot_id: uuid.UUID
    quantity: ReservationQty = 1
    idempotency_key: IdempotencyKey | None = None


class ReservationRead(BaseModel):
    id: uuid.UUID
    slot_id: uuid.UUID
    machine_id: uuid.UUID
    product_sku: str
    product_name: str
    quantity: int
    unit_price_cents: int
    total_cents: int
    currency: str
    # Effective status: a hold past its expiry reads "expired" even before the
    # expiry sweeper has recorded it.
    status: ReservationStatus
    expires_at: datetime
    created_at: datetime


class OrderRead(ReadModel):
    id: uuid.UUID
    reservation_id: uuid.UUID
    amount_cents: int
    currency: str
    payment_status: PaymentStatus
    created_at: datetime
