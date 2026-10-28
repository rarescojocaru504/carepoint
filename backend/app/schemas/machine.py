import uuid
from typing import Annotated, Self

from pydantic import Field, model_validator

from app.models.enums import MachineStatus
from app.schemas.common import GeoPoint, InputModel, NonNegativeQty

SerialNumber = Annotated[str, Field(pattern=r"^[A-Z0-9-]{4,32}$")]
SlotCode = Annotated[str, Field(pattern=r"^[A-Z][0-9]{1,2}$")]
SlotCapacity = Annotated[int, Field(strict=True, gt=0, le=100)]


class MachineCreate(InputModel):
    serial_number: SerialNumber
    name: Annotated[str, Field(min_length=1, max_length=100)]
    address: Annotated[str, Field(min_length=1, max_length=255)]
    location: GeoPoint
    status: MachineStatus = MachineStatus.ACTIVE


class SlotCreate(InputModel):
    slot_code: SlotCode
    product_id: uuid.UUID | None = None
    capacity: SlotCapacity
    quantity: NonNegativeQty = 0
    low_stock_threshold: NonNegativeQty = 2

    @model_validator(mode="after")
    def _check_bounds(self) -> Self:
        if self.quantity > self.capacity:
            raise ValueError("quantity cannot exceed capacity")
        if self.low_stock_threshold > self.capacity:
            raise ValueError("low_stock_threshold cannot exceed capacity")
        if self.product_id is None and self.quantity != 0:
            raise ValueError("an empty slot (no product_id) cannot hold stock")
        return self
