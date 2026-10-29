import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.enums import (
    DispenseCommandStatus,
    MachineErrorCode,
    PaymentStatus,
    ReservationStatus,
    db_enum,
)
from app.models.machine import MachineSlot
from app.models.mixins import Timestamps, UUIDPrimaryKey


class Reservation(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "reservations"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key"),
        CheckConstraint("quantity > 0 AND quantity <= 10", name="quantity_in_range"),
        CheckConstraint("unit_price_cents > 0", name="unit_price_positive"),
        # Availability = slot quantity - active holds; this index serves that sum.
        Index("ix_reservations_slot_id_status", "slot_id", "status"),
        # The expiry sweeper scans held reservations by expiry time.
        Index("ix_reservations_status_expires_at", "status", "expires_at"),
        # A pickup code is looked up by its hash, and no two *active* (paid)
        # reservations may share one. The hash covers machine id + code, so this
        # also means codes are unique per machine while in use (ADR-016).
        Index(
            "uq_reservations_active_pickup_code",
            "pickup_code_hash",
            unique=True,
            postgresql_where=text("status = 'paid'"),
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    slot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("machine_slots.id", ondelete="RESTRICT"), nullable=False
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    # Price snapshot at reservation time, so later catalogue changes don't alter it.
    unit_price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[ReservationStatus] = mapped_column(
        db_enum(ReservationStatus, "reservation_status"),
        nullable=False,
        default=ReservationStatus.HELD,
        server_default=ReservationStatus.HELD.value,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    pickup_code_hash: Mapped[str | None] = mapped_column(String(128))
    idempotency_key: Mapped[str | None] = mapped_column(String(64))

    slot: Mapped[MachineSlot] = relationship()


class Order(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint("amount_cents > 0", name="amount_positive"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency_iso4217"),
    )

    reservation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("reservations.id", ondelete="RESTRICT"), unique=True, nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    payment_status: Mapped[PaymentStatus] = mapped_column(
        db_enum(PaymentStatus, "payment_status"), nullable=False
    )
    payment_reference: Mapped[str | None] = mapped_column(String(64))

    reservation: Mapped[Reservation] = relationship()


class DispenseCommand(UUIDPrimaryKey, Timestamps, Base):
    """A `dispense` sent to a machine for a reservation (ADR-018).

    Its id is the command id on the wire, so a machine's result, resent after
    any number of reconnects, always finds the one command it belongs to.
    """

    __tablename__ = "dispense_commands"
    __table_args__ = (
        CheckConstraint("quantity > 0 AND quantity <= 10", name="quantity_in_range"),
        CheckConstraint(
            "units_dispensed IS NULL OR (units_dispensed >= 0 AND units_dispensed <= quantity)",
            name="units_dispensed_in_range",
        ),
        # At most one dispense in flight per reservation.
        Index(
            "uq_dispense_commands_open_reservation",
            "reservation_id",
            unique=True,
            postgresql_where=text("status = 'open'"),
        ),
        Index("ix_dispense_commands_machine_id_status", "machine_id", "status"),
    )

    machine_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("machines.id", ondelete="RESTRICT"), nullable=False
    )
    reservation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("reservations.id", ondelete="RESTRICT"), nullable=False
    )
    slot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("machine_slots.id", ondelete="RESTRICT"), nullable=False
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[DispenseCommandStatus] = mapped_column(
        db_enum(DispenseCommandStatus, "dispense_command_status"),
        nullable=False,
        default=DispenseCommandStatus.OPEN,
        server_default=DispenseCommandStatus.OPEN.value,
    )
    units_dispensed: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[MachineErrorCode | None] = mapped_column(
        db_enum(MachineErrorCode, "machine_error_code")
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
