import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field

from app.models.enums import PaymentStatus, ReservationStatus
from app.schemas.common import InputModel

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


class CheckoutRequest(InputModel):
    # A provider payment-method token (never a raw card number). The mock
    # provider accepts pm_card_ok, pm_card_declined and pm_card_provider_error.
    payment_method: Annotated[str, Field(pattern=r"^pm_[a-z0-9_]{2,60}$")]


class OrderRead(BaseModel):
    id: uuid.UUID
    reservation_id: uuid.UUID
    product_sku: str
    product_name: str
    quantity: int
    amount_cents: int
    currency: str
    payment_status: PaymentStatus
    reservation_status: ReservationStatus
    created_at: datetime
    updated_at: datetime


class CheckoutResponse(BaseModel):
    reservation: ReservationRead
    order: OrderRead
