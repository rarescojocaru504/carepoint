"""SQLAlchemy models.

Every model module must be imported here so that `Base.metadata` is complete
when Alembic (see `alembic/env.py`) imports this package for autogenerate.
"""

from app.models.auth import RefreshToken
from app.models.enums import (
    DispenseCommandStatus,
    MachineErrorCode,
    MachineEventKind,
    MachineStatus,
    PaymentStatus,
    ProductCategory,
    ReservationStatus,
    StockMovementReason,
    UserRole,
)
from app.models.inventory import StockMovement
from app.models.machine import Machine, MachineEvent, MachineSlot
from app.models.product import Product
from app.models.reservation import DispenseCommand, Order, Reservation
from app.models.user import User

__all__ = [
    "DispenseCommand",
    "DispenseCommandStatus",
    "Machine",
    "MachineErrorCode",
    "MachineEvent",
    "MachineEventKind",
    "MachineSlot",
    "MachineStatus",
    "Order",
    "PaymentStatus",
    "Product",
    "ProductCategory",
    "RefreshToken",
    "Reservation",
    "ReservationStatus",
    "StockMovement",
    "StockMovementReason",
    "User",
    "UserRole",
]
