import uuid
from datetime import datetime
from typing import Annotated

from pydantic import Field

from app.models.enums import PaymentStatus, ReservationStatus
from app.schemas.common import InputModel, ReadModel

ReservationQty = Annotated[int, Field(strict=True, gt=0, le=10)]
IdempotencyKey = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{8,64}$")]


class ReservationCreate(InputModel):
    slot_id: uuid.UUID
    quantity: ReservationQty = 1
    idempotency_key: IdempotencyKey | None = None


class ReservationRead(ReadModel):
    id: uuid.UUID
    slot_id: uuid.UUID
    quantity: int
    unit_price_cents: int
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
