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
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.enums import PaymentStatus, ReservationStatus, db_enum
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
