import uuid
from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from app.models.enums import StockMovementReason
from app.schemas.common import InputModel, ReadModel

StockDelta = Annotated[int, Field(strict=True, ge=-1000, le=1000)]


class StockAdjustmentCreate(InputModel):
    """Operator-initiated stock change.

    Only `restock` and `adjustment` may come from a request. `initial`,
    `dispense` and `telemetry_correction` are produced by the system itself.
    """

    slot_id: uuid.UUID
    delta: StockDelta
    reason: Literal[StockMovementReason.RESTOCK, StockMovementReason.ADJUSTMENT]
    note: Annotated[str, Field(max_length=500)] | None = None

    @model_validator(mode="after")
    def _check_delta(self) -> Self:
        if self.delta == 0:
            raise ValueError("delta must be non-zero")
        return self


class StockMovementRead(ReadModel):
    id: int
    slot_id: uuid.UUID
    delta: int
    quantity_after: int
    reason: StockMovementReason
    actor_user_id: uuid.UUID | None
    reservation_id: uuid.UUID | None
    note: str | None
    created_at: datetime
