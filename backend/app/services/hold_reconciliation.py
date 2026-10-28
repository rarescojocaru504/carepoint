"""Bring Redis hold state back in line with Postgres (ADR-013).

Drift can come from a Redis release that failed after a committed cancel, a
process that died between the Redis hold and the Postgres insert, a Redis
restart without persistence, or a stock change whose mirror update failed.
Reconciliation runs at startup and periodically, and only ever applies a diff:

- holds Postgres has (held/paid, unexpired) but Redis lacks -> added;
- Redis holds whose reservation is finished in Postgres -> removed;
- Redis holds with no Postgres row at all, older than the orphan grace period
  -> removed (younger ones may be an insert still in flight, so they stay);
- the physical-quantity mirror -> compare-and-set to Postgres's value.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import MachineSlot, Reservation
from app.services import hold_store
from app.services.reservations import load_slot_state

logger = logging.getLogger(__name__)


@dataclass
class ReconcileReport:
    slots_checked: int = 0
    slots_initialised: int = 0
    holds_added: int = 0
    holds_removed: int = 0
    quantities_fixed: int = 0


async def reconcile_slot(
    session: AsyncSession, redis: Redis, slot_id: uuid.UUID, report: ReconcileReport
) -> None:
    settings = get_settings()
    redis_state = await hold_store.read_state(redis, slot_id)
    db_state = await load_slot_state(session, slot_id)
    report.slots_checked += 1

    if not redis_state.initialised:
        # Nothing to diff against: build it outright (no-op if a concurrent
        # reserve initialised it first).
        if await hold_store.initialise(redis, slot_id, db_state):
            report.slots_initialised += 1
        return

    db_holds = {hold.reservation_id: hold for hold in db_state.holds}
    unknown = [rid for rid in redis_state.holds if rid not in db_holds]
    has_row = (
        set(await session.scalars(select(Reservation.id).where(Reservation.id.in_(unknown))))
        if unknown
        else set()
    )
    # A Redis hold's creation time is its expiry minus the hold length.
    orphan_cutoff = datetime.now(UTC) - timedelta(seconds=settings.hold_orphan_grace_seconds)
    hold_length = timedelta(seconds=settings.reservation_hold_seconds)
    remove = [
        rid
        for rid in unknown
        if rid in has_row  # finished in Postgres (cancelled, expired, dispensed)
        or redis_state.holds[rid].expires_at - hold_length < orphan_cutoff  # stale orphan
    ]
    add = [hold for rid, hold in db_holds.items() if rid not in redis_state.holds]

    if not remove and not add and redis_state.physical == db_state.physical_quantity:
        return
    result = await hold_store.apply_reconciliation(
        redis,
        slot_id,
        expected_quantity=redis_state.physical,
        new_quantity=db_state.physical_quantity,
        remove=remove,
        add=add,
    )
    report.holds_added += result.added
    report.holds_removed += result.removed
    report.quantities_fixed += int(result.quantity_updated)
    logger.warning(
        "Reconciled slot %s: +%d holds, -%d holds, quantity %s",
        slot_id,
        result.added,
        result.removed,
        f"{redis_state.physical}->{db_state.physical_quantity}"
        if result.quantity_updated
        else "unchanged",
    )


async def reconcile_all(session: AsyncSession, redis: Redis) -> ReconcileReport:
    report = ReconcileReport()
    for slot_id in list(await session.scalars(select(MachineSlot.id).order_by(MachineSlot.id))):
        await reconcile_slot(session, redis, slot_id, report)
    return report
