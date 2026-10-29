"""Business gauges for GET /metrics (ADR-029), read from the database when
the metrics are scraped. At MVP scale (tens of machines) these are a few
cheap aggregate queries, and they always agree with the dashboard's KPIs."""

from datetime import UTC, datetime

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.metrics import MACHINES, RESERVATIONS, UNITS_AVAILABLE
from app.models import Machine, MachineSlot, MachineStatus, Product, Reservation, ReservationStatus
from app.services.availability import ACTIVE_HOLD_STATUSES, active_holds_by_slot
from app.services.machine_status import is_online


async def refresh_business_gauges(session: AsyncSession) -> None:
    window = get_settings().machine_online_window_seconds
    now = datetime.now(UTC)

    machines = await session.scalars(
        select(Machine).where(Machine.status != MachineStatus.DECOMMISSIONED)
    )
    states = {"online": 0, "offline": 0, "maintenance": 0}
    for machine in machines:
        if machine.status is MachineStatus.MAINTENANCE:
            states["maintenance"] += 1
        elif is_online(machine, window, now=now):
            states["online"] += 1
        else:
            states["offline"] += 1
    for state, count in states.items():
        MACHINES.labels(state).set(count)

    lapsed = Reservation.status.in_(ACTIVE_HOLD_STATUSES) & (Reservation.expires_at <= func.now())
    effective = case((lapsed, ReservationStatus.EXPIRED.value), else_=Reservation.status)
    counts = dict(
        (await session.execute(select(effective, func.count()).group_by(effective))).tuples().all()
    )
    for status in ReservationStatus:
        RESERVATIONS.labels(status.value).set(counts.get(status.value, 0))

    holds = active_holds_by_slot()
    available = await session.scalar(
        select(
            func.coalesce(
                func.sum(func.greatest(MachineSlot.quantity - func.coalesce(holds.c.held, 0), 0)),
                0,
            )
        )
        .join(Machine, Machine.id == MachineSlot.machine_id)
        .join(Product, Product.id == MachineSlot.product_id)
        .outerjoin(holds, holds.c.slot_id == MachineSlot.id)
        .where(Machine.status == MachineStatus.ACTIVE, Product.is_active.is_(True))
    )
    UNITS_AVAILABLE.set(int(available or 0))
