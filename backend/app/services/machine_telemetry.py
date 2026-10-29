"""What connected machines report: liveness, door and temperature, faults and
stock counts (ADR-017). Each function is one short transaction and commits.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Machine,
    MachineErrorCode,
    MachineEvent,
    MachineEventKind,
    MachineSlot,
    StockMovementReason,
)
from app.schemas.machine_protocol import ProtocolErrorCode
from app.services import hold_store
from app.services.inventory import record_stock_movement
from app.services.pickups import slots_in_flight

logger = logging.getLogger(__name__)


class StockReportError(Exception):
    """The report was rejected as a whole; nothing of it was applied."""

    def __init__(self, code: ProtocolErrorCode, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class StockCorrection:
    slot_code: str
    previous: int
    reported: int


async def record_connection(
    session: AsyncSession, machine_id: uuid.UUID, kind: MachineEventKind, *, offline: bool = True
) -> None:
    """Log a connect or disconnect; a disconnect also marks the machine offline.

    `offline=False` is a superseded connection closing while a newer one is
    live: the machine never went away, so nothing is marked or logged (the
    event log is what uptime is computed from).
    """
    if kind is MachineEventKind.CONNECTED:
        await session.execute(
            update(Machine).where(Machine.id == machine_id).values(last_seen_at=func.now())
        )
    elif not offline:
        return
    else:
        await session.execute(
            update(Machine).where(Machine.id == machine_id).values(disconnected_at=func.now())
        )
    session.add(MachineEvent(machine_id=machine_id, kind=kind))
    await session.commit()


async def record_heartbeat(
    session: AsyncSession, machine_id: uuid.UUID, firmware_version: str
) -> None:
    await session.execute(
        update(Machine)
        .where(Machine.id == machine_id)
        .values(last_seen_at=func.now(), firmware_version=firmware_version)
    )
    await session.commit()


async def record_telemetry(
    session: AsyncSession, machine_id: uuid.UUID, *, door_open: bool, temperature_c: float
) -> None:
    """Store the latest readings; a door change is also logged as an event."""
    machine = await session.scalar(
        select(Machine)
        .where(Machine.id == machine_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if machine is None:
        raise LookupError(str(machine_id))
    if machine.door_open is not door_open:
        kind = MachineEventKind.DOOR_OPENED if door_open else MachineEventKind.DOOR_CLOSED
        session.add(MachineEvent(machine_id=machine_id, kind=kind))
    machine.door_open = door_open
    machine.temperature_c = Decimal(f"{temperature_c:.1f}")
    machine.telemetry_at = datetime.now(UTC)
    await session.commit()


async def record_error(
    session: AsyncSession,
    machine_id: uuid.UUID,
    *,
    code: MachineErrorCode,
    slot_code: str | None,
    message: str | None,
) -> None:
    session.add(
        MachineEvent(
            machine_id=machine_id,
            kind=MachineEventKind.ERROR,
            error_code=code,
            slot_code=slot_code,
            message=message,
        )
    )
    await session.commit()
    logger.warning(
        "Machine %s reported %s (slot=%s): %s", machine_id, code.value, slot_code, message
    )


async def reconcile_stock_report(
    session: AsyncSession, redis: Redis, machine_id: uuid.UUID, counts: dict[str, int]
) -> list[StockCorrection]:
    """Make the database agree with what the machine physically counted.

    The machine is the only witness of what is in its slots, so a mismatch is
    corrected towards its count and recorded in the ledger as a
    `telemetry_correction`. The whole report is validated first and applied in
    one transaction, or rejected as a whole: a count for a slot this machine
    does not have, above the slot's capacity, or of items in a slot with no
    product assigned. A rejection raises before anything is written (the
    caller's session just ends, releasing the row locks).
    """
    slots = {
        slot.slot_code: slot
        for slot in await session.scalars(
            select(MachineSlot)
            .where(MachineSlot.machine_id == machine_id, MachineSlot.slot_code.in_(counts))
            .order_by(MachineSlot.id)  # consistent lock order
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    }
    unknown = sorted(set(counts) - set(slots))
    if unknown:
        raise StockReportError(
            ProtocolErrorCode.UNKNOWN_SLOT, f"no such slot: {', '.join(unknown)}"
        )
    for code, reported in counts.items():
        slot = slots[code]
        if reported > slot.capacity:
            raise StockReportError(
                ProtocolErrorCode.OUT_OF_RANGE,
                f"slot {code}: {reported} exceeds capacity {slot.capacity}",
            )
        if slot.product_id is None and reported > 0:
            raise StockReportError(
                ProtocolErrorCode.SLOT_NOT_CONFIGURED, f"slot {code} has no product assigned"
            )

    # A slot with a dispense under way is skipped: its count is in motion, and
    # the dispense result, not this report, accounts for the units (ADR-018).
    in_flight = await slots_in_flight(session, [slot.id for slot in slots.values()])
    corrections: list[tuple[uuid.UUID, StockCorrection]] = []
    for code, reported in sorted(counts.items()):
        slot = slots[code]
        if reported == slot.quantity or slot.id in in_flight:
            continue
        correction = StockCorrection(code, slot.quantity, reported)
        await record_stock_movement(
            session,
            slot_id=slot.id,
            delta=reported - slot.quantity,
            reason=StockMovementReason.TELEMETRY_CORRECTION,
            note=f"machine counted {reported}, database had {slot.quantity}",
        )
        corrections.append((slot.id, correction))
    await session.commit()

    for slot_id, correction in corrections:
        logger.warning(
            "Stock corrected from machine %s slot %s: %d -> %d",
            machine_id,
            correction.slot_code,
            correction.previous,
            correction.reported,
        )
        try:
            await hold_store.apply_stock_delta(
                redis, slot_id, correction.reported - correction.previous
            )
        except RedisError:
            # Postgres is right; reconciliation brings the mirror back (ADR-013).
            logger.exception("Could not mirror stock correction for slot %s", slot_id)
    return [correction for _, correction in corrections]
