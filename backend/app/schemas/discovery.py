import uuid
from datetime import datetime

from pydantic import BaseModel

from app.models.enums import MachineStatus
from app.schemas.product import ProductRead


class Location(BaseModel):
    lat: float
    lng: float


class MachineSummary(BaseModel):
    id: uuid.UUID
    name: str
    address: str
    location: Location
    status: MachineStatus
    online: bool
    distance_m: float
    products_available: int
    units_available: int


class SlotAvailability(BaseModel):
    slot_id: uuid.UUID
    slot_code: str
    product: ProductRead
    available: int
    low_stock: bool


class MachineDetail(BaseModel):
    id: uuid.UUID
    name: str
    address: str
    location: Location
    status: MachineStatus
    online: bool
    last_seen_at: datetime | None
    slots: list[SlotAvailability]


class ProductLocation(BaseModel):
    machine_id: uuid.UUID
    machine_name: str
    address: str
    location: Location
    online: bool
    distance_m: float
    available: int
    # The slot to reserve from: the one holding the most available units.
    slot_id: uuid.UUID


class ProductNearby(BaseModel):
    product: ProductRead
    locations: list[ProductLocation]
