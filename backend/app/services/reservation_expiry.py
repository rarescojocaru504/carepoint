"""Expiry sweeper: record lapsed holds as `expired` (ADR-013).

Bookkeeping, not correctness: availability already ignores holds past their
`expires_at` (ADR-011/012) the moment they lapse. The sweeper makes Postgres
say so explicitly, tidies Redis, and is the hook where Phase 5 voids payments.
"""

import logging
import uuid

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DispenseCommand, DispenseCommandStatus, Reservation, ReservationStatus
from app.services import events, hold_store
from app.services.availability import ACTIVE_HOLD_STATUSES
from app.services.events import Topic

logger = logging.getLogger(__name__)


async def expire_due_reservations(
    session: AsyncSession, redis: Redis, *, batch_size: int = 500
) -> list[uuid.UUID]:
    """Mark up to `batch_size` lapsed holds expired. Returns their ids.

    `FOR UPDATE SKIP LOCKED`: a reservation locked by another transaction (a
    dispense in flight, a cancel) is skipped, never waited on or overridden;
    whatever that transaction decides wins, and a later pass re-checks it.
    Safe to run concurrently on several app instances.
    """
    # The batch is chosen in a MATERIALIZED CTE so it is evaluated exactly once.
    # As `WHERE id IN (SELECT ... LIMIT n FOR UPDATE SKIP LOCKED)` the planner
    # may pick a nested-loop semi-join that re-runs the locking subquery per
    # outer row, and the LIMIT then no longer bounds the update (we hit this).
    due = (
        select(Reservation.id)
        .where(
            Reservation.status.in_(ACTIVE_HOLD_STATUSES),
            Reservation.expires_at <= func.now(),
        )
        .order_by(Reservation.expires_at)
        .limit(batch_size)
        .with_for_update(skip_locked=True)
        .cte("due")
        .prefix_with("MATERIALIZED")
    )
    rows = (
        await session.execute(
            update(Reservation)
            # Re-checked against the row's latest version if it changed meanwhile
            # (READ COMMITTED), so a just-dispensed reservation is never expired.
            .where(Reservation.id == due.c.id, Reservation.status.in_(ACTIVE_HOLD_STATUSES))
            .values(status=ReservationStatus.EXPIRED)
            .returning(Reservation.id, Reservation.slot_id, Reservation.user_id)
            .execution_options(synchronize_session=False)
        )
    ).all()
    if rows:
        # A dispense that got no result before the (extended) hold ran out is
        # abandoned with it; a result arriving later is recorded as late (ADR-018).
        # Same lock order as everywhere: reservation rows, then their commands.
        await session.execute(
            update(DispenseCommand)
            .where(
                DispenseCommand.reservation_id.in_([row.id for row in rows]),
                DispenseCommand.status == DispenseCommandStatus.OPEN,
            )
            .values(status=DispenseCommandStatus.ABANDONED, completed_at=func.now())
            .execution_options(synchronize_session=False)
        )
    await session.commit()

    for row in rows:
        try:
            await hold_store.release(redis, row.slot_id, row.id)
        except RedisError:
            # Redis already ignores the lapsed hold; reconciliation removes it later.
            logger.warning("Could not tidy expired hold %s in Redis", row.id)
    if rows:
        logger.info("Expired %d reservation(s)", len(rows))
        await events.publish(redis, Topic.RESERVATION, machine_id=None, change="expired")
        for row in rows:  # each owner hears about their own
            await events.publish_to_owner(
                redis, user_id=row.user_id, reservation_id=row.id, machine_id=None,
                change="expired",
            )  # fmt: skip
    return [row.id for row in rows]
