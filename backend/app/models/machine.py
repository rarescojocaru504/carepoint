import uuid
from datetime import datetime
from decimal import Decimal

from geoalchemy2 import Geography, WKBElement
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.enums import MachineErrorCode, MachineEventKind, MachineStatus, db_enum
from app.models.mixins import Timestamps, UUIDPrimaryKey
from app.models.product import Product


class Machine(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "machines"
    __table_args__ = (
        CheckConstraint("serial_number ~ '^[A-Z0-9-]{4,32}$'", name="serial_format"),
        CheckConstraint(
            "auth_public_key IS NULL OR octet_length(auth_public_key) = 32",
            name="ed25519_key_length",
        ),
        # Explicit GiST index (instead of GeoAlchemy2's implicit one) so Alembic
        # tracks it like any other index.
        Index("ix_machines_location", "location", postgresql_using="gist"),
        CheckConstraint(
            "temperature_c IS NULL OR temperature_c BETWEEN -40 AND 85",
            name="temperature_in_range",
        ),
    )

    serial_number: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    address: Mapped[str] = mapped_column(String(255), nullable=False)
    location: Mapped[WKBElement] = mapped_column(
        Geography(geometry_type="POINT", srid=4326, spatial_index=False),
        nullable=False,
    )
    status: Mapped[MachineStatus] = mapped_column(
        db_enum(MachineStatus, "machine_status"),
        nullable=False,
        default=MachineStatus.ACTIVE,
        server_default=MachineStatus.ACTIVE.value,
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    firmware_version: Mapped[str | None] = mapped_column(String(32))
    # Raw 32-byte Ed25519 public key. The private key never leaves the machine
    # (see ADR-007), so a database leak cannot be used to impersonate machines.
    auth_public_key: Mapped[bytes | None] = mapped_column(LargeBinary(32))
    # Latest telemetry reported over the machine gateway (ADR-017).
    door_open: Mapped[bool | None] = mapped_column(Boolean)
    temperature_c: Mapped[Decimal | None] = mapped_column(Numeric(4, 1))
    telemetry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    slots: Mapped[list["MachineSlot"]] = relationship(
        back_populates="machine", order_by="MachineSlot.slot_code"
    )


class MachineSlot(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "machine_slots"
    __table_args__ = (
        UniqueConstraint("machine_id", "slot_code"),
        CheckConstraint("slot_code ~ '^[A-Z][0-9]{1,2}$'", name="slot_code_format"),
        CheckConstraint("capacity > 0", name="capacity_positive"),
        CheckConstraint("quantity >= 0 AND quantity <= capacity", name="quantity_in_range"),
        CheckConstraint(
            "low_stock_threshold >= 0 AND low_stock_threshold <= capacity",
            name="threshold_in_range",
        ),
        CheckConstraint("product_id IS NOT NULL OR quantity = 0", name="empty_slot_no_stock"),
    )

    machine_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("machines.id", ondelete="RESTRICT"), nullable=False
    )
    product_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), index=True
    )
    slot_code: Mapped[str] = mapped_column(String(3), nullable=False)
    # Physical units in the slot. Only changed through services.inventory, which
    # writes a matching stock_movements row in the same transaction.
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    capacity: Mapped[int] = mapped_column(Integer, nullable=False)
    low_stock_threshold: Mapped[int] = mapped_column(
        Integer, nullable=False, default=2, server_default="2"
    )

    machine: Mapped[Machine] = relationship(back_populates="slots")
    product: Mapped[Product | None] = relationship()


class MachineEvent(Base):
    """Append-only log of what a machine did: connections, door changes, faults.

    Feeds the operator dashboard (uptime, error feed) in Phase 8.
    """

    __tablename__ = "machine_events"
    __table_args__ = (
        Index("ix_machine_events_machine_id_created_at", "machine_id", "created_at"),
        CheckConstraint("(kind = 'error') = (error_code IS NOT NULL)", name="error_code_iff_error"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    machine_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("machines.id", ondelete="RESTRICT"), nullable=False
    )
    kind: Mapped[MachineEventKind] = mapped_column(
        db_enum(MachineEventKind, "machine_event_kind"), nullable=False
    )
    error_code: Mapped[MachineErrorCode | None] = mapped_column(
        db_enum(MachineErrorCode, "machine_error_code")
    )
    slot_code: Mapped[str | None] = mapped_column(String(3))
    message: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
