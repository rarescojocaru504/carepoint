from enum import StrEnum

from sqlalchemy import Enum


class UserRole(StrEnum):
    CUSTOMER = "customer"
    OPERATOR = "operator"
    ADMIN = "admin"


class MachineStatus(StrEnum):
    """Operational status set by operators. Online/offline is derived from `last_seen_at`."""

    ACTIVE = "active"
    MAINTENANCE = "maintenance"
    DECOMMISSIONED = "decommissioned"


class ProductCategory(StrEnum):
    HYGIENE = "hygiene"
    MENSTRUAL_CARE = "menstrual_care"
    SEXUAL_HEALTH = "sexual_health"
    FIRST_AID = "first_aid"
    PHARMACY = "pharmacy"
    BABY_CARE = "baby_care"
    ESSENTIALS = "essentials"


class StockMovementReason(StrEnum):
    INITIAL = "initial"
    RESTOCK = "restock"
    DISPENSE = "dispense"
    ADJUSTMENT = "adjustment"
    TELEMETRY_CORRECTION = "telemetry_correction"


class ReservationStatus(StrEnum):
    HELD = "held"
    PAID = "paid"
    DISPENSED = "dispensed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class MachineEventKind(StrEnum):
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    ERROR = "error"
    DOOR_OPENED = "door_opened"
    DOOR_CLOSED = "door_closed"


class MachineErrorCode(StrEnum):
    """Faults a machine can report (docs/machine-protocol.md)."""

    JAM = "jam"
    MOTOR_FAULT = "motor_fault"
    SENSOR_FAULT = "sensor_fault"
    DOOR_FORCED = "door_forced"
    TEMPERATURE_OUT_OF_RANGE = "temperature_out_of_range"
    POWER_LOSS = "power_loss"
    OTHER = "other"


class PaymentStatus(StrEnum):
    AUTHORIZED = "authorized"
    CAPTURED = "captured"
    VOIDED = "voided"
    FAILED = "failed"


def db_enum(enum_cls: type[StrEnum], name: str) -> Enum:
    """Store enums as VARCHAR + CHECK constraint (not a native PG enum).

    Adding a value later is then a constraint swap in a migration instead of an
    `ALTER TYPE`, which cannot run inside a transaction on older PostgreSQL.
    """
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=32,
        values_callable=lambda members: [member.value for member in members],
        validate_strings=True,
    )
