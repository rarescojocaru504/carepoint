"""Dashboard KPIs (ADR-021). Computed on request from the tables themselves;
at MVP scale (tens of machines, thousands of reservations) that is cheap and
always consistent with what the rest of the dashboard shows."""

import uuid
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import (
    Machine,
    MachineEvent,
    MachineEventKind,
    MachineSlot,
    MachineStatus,
    Reservation,
    ReservationStatus,
    StockMovement,
    StockMovementReason,
)
from app.schemas.admin import KpiSummary
from app.services.availability import ACTIVE_HOLD_STATUSES
from app.services.machine_status import is_online


def connected_share(
    events: Sequence[tuple[datetime, MachineEventKind]],
    *,
    initially_connected: bool,
    start: datetime,
    end: datetime,
    last_seen_at: datetime | None,
    online_window: timedelta,
) -> float:
    """Share of [start, end] a machine was connected, from its connect and
    disconnect events (in time order). A connection that ended without a
    disconnect event (a crash) counts only until the online window after the
    machine was last heard from."""
    up = timedelta()
    connected, since = initially_connected, start
    for at, kind in events:
        at = min(max(at, start), end)
        if connected:
            up += at - since
        connected, since = kind is MachineEventKind.CONNECTED, at
    if connected:
        until = end if last_seen_at is None else min(end, last_seen_at + online_window)
        up += max(until - since, timedelta())
    return up / (end - start)


async def _uptime(
    session: AsyncSession, machines: Sequence[Machine], start: datetime, end: datetime
) -> float | None:
    if not machines:
        return None
    ids = [m.id for m in machines]
    connection_kinds = (MachineEventKind.CONNECTED, MachineEventKind.DISCONNECTED)
    rows = await session.execute(
        select(MachineEvent.machine_id, MachineEvent.created_at, MachineEvent.kind)
        .where(
            MachineEvent.machine_id.in_(ids),
            MachineEvent.kind.in_(connection_kinds),
            MachineEvent.created_at >= start,
        )
        .order_by(MachineEvent.created_at, MachineEvent.id)
    )
    events: dict[uuid.UUID, list[tuple[datetime, MachineEventKind]]] = defaultdict(list)
    for machine_id, at, kind in rows:
        events[machine_id].append((at, kind))
    # The state each machine was in when the window opened.
    last_before = (
        select(MachineEvent.machine_id, func.max(MachineEvent.id).label("id"))
        .where(
            MachineEvent.machine_id.in_(ids),
            MachineEvent.kind.in_(connection_kinds),
            MachineEvent.created_at < start,
        )
        .group_by(MachineEvent.machine_id)
        .subquery()
    )
    initial = {
        machine_id: kind is MachineEventKind.CONNECTED
        for machine_id, kind in await session.execute(
            select(MachineEvent.machine_id, MachineEvent.kind).join(
                last_before, last_before.c.id == MachineEvent.id
            )
        )
    }
    window = timedelta(seconds=get_settings().machine_online_window_seconds)
    shares = [
        connected_share(
            events[m.id],
            initially_connected=initial.get(m.id, False),
            start=start,
            end=end,
            last_seen_at=m.last_seen_at,
            online_window=window,
        )
        for m in machines
    ]
    return sum(shares) / len(shares)


async def _created_since(session: AsyncSession, since: datetime) -> int:
    count = await session.scalar(select(func.count()).where(Reservation.created_at >= since))
    return int(count or 0)


async def kpi_summary(session: AsyncSession, *, now: datetime | None = None) -> KpiSummary:
    now = now or datetime.now(UTC)
    day, week = now - timedelta(hours=24), now - timedelta(days=7)

    effective = case(
        (
            and_(Reservation.status.in_(ACTIVE_HOLD_STATUSES), Reservation.expires_at <= now),
            ReservationStatus.EXPIRED.value,
        ),
        else_=Reservation.status,
    )
    outcomes = {
        status: int(count)
        for status, count in await session.execute(
            select(effective, func.count())
            .where(Reservation.created_at >= week)
            .group_by(effective)
        )
    }
    dispensed = outcomes.get(ReservationStatus.DISPENSED.value, 0)
    expired = outcomes.get(ReservationStatus.EXPIRED.value, 0)
    finished = dispensed + expired + outcomes.get(ReservationStatus.CANCELLED.value, 0)

    avg_pickup = await session.scalar(
        select(func.avg(func.extract("epoch", StockMovement.created_at - Reservation.created_at)))
        .join(Reservation, Reservation.id == StockMovement.reservation_id)
        .where(
            StockMovement.reason == StockMovementReason.DISPENSE,
            Reservation.status == ReservationStatus.DISPENSED,
            Reservation.created_at >= week,
        )
    )

    in_service = and_(MachineSlot.product_id.is_not(None), Machine.status == MachineStatus.ACTIVE)
    slot_counts = (
        await session.execute(
            select(
                func.count().filter(MachineSlot.quantity == 0),
                func.count().filter(
                    MachineSlot.quantity > 0,
                    MachineSlot.quantity <= MachineSlot.low_stock_threshold,
                ),
            )
            .select_from(MachineSlot)
            .join(Machine, Machine.id == MachineSlot.machine_id)
            .where(in_service)
        )
    ).one()

    machines = list(
        await session.scalars(select(Machine).where(Machine.status != MachineStatus.DECOMMISSIONED))
    )
    window = get_settings().machine_online_window_seconds
    return KpiSummary(
        generated_at=now,
        reservations_24h=await _created_since(session, day),
        reservations_7d=await _created_since(session, week),
        pickup_rate_7d=dispensed / finished if finished else None,
        expired_7d=expired,
        avg_hold_to_pickup_seconds_7d=float(avg_pickup) if avg_pickup is not None else None,
        stock_outs=int(slot_counts[0]),
        low_stock_slots=int(slot_counts[1]),
        machines_online=sum(is_online(m, window, now=now) for m in machines),
        machines_total=len(machines),
        uptime_24h=await _uptime(session, machines, day, now),
    )
