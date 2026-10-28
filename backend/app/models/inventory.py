import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.enums import StockMovementReason, db_enum


class StockMovement(Base):
    """Append-only ledger of every change to a slot's physical quantity.

    A database trigger (migration 0002) rejects UPDATE, DELETE and TRUNCATE, so
    history cannot be rewritten even by a buggy or compromised service.
    """

    __tablename__ = "stock_movements"
    __table_args__ = (
        CheckConstraint("delta <> 0", name="delta_nonzero"),
        CheckConstraint("quantity_after >= 0", name="quantity_after_nonnegative"),
        Index("ix_stock_movements_slot_id_created_at", "slot_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    slot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("machine_slots.id", ondelete="RESTRICT"), nullable=False
    )
    delta: Mapped[int] = mapped_column(Integer, nullable=False)
    quantity_after: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[StockMovementReason] = mapped_column(
        db_enum(StockMovementReason, "stock_movement_reason"), nullable=False
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    reservation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("reservations.id", ondelete="RESTRICT")
    )
    note: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
