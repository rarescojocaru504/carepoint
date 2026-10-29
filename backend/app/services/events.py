"""Live change notices for operator dashboards (ADR-022).

Services publish a small notice after they commit a change: which kind of
thing changed (`topic`) and, where known, at which machine. Notices carry no
data: a dashboard re-reads what it shows through the normal admin API, so it
can never drift from it and access control stays in one place.

Publishing is best effort. A notice lost to a Redis hiccup costs a dashboard
one stale view until the next notice or refresh, never a failed operation.
"""

import json
import logging
import uuid
from datetime import UTC, datetime
from enum import StrEnum

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MachineSlot

logger = logging.getLogger(__name__)

CHANNEL = "cp:events"


class Topic(StrEnum):
    MACHINE = "machine"  # connected, disconnected, telemetry, fault, status
    STOCK = "stock"  # a slot's physical count changed
    RESERVATION = "reservation"  # created, paid, cancelled, expired, dispensed


async def publish(redis: Redis, topic: Topic, *, machine_id: uuid.UUID | None, change: str) -> None:
    """`machine_id` None means "possibly several machines": refresh them all."""
    notice = {
        "topic": topic.value,
        "machine_id": str(machine_id) if machine_id is not None else None,
        "change": change,
        "at": datetime.now(UTC).isoformat(),
    }
    try:
        await redis.publish(CHANNEL, json.dumps(notice))
    except RedisError:
        logger.warning("Could not publish %s/%s notice", topic.value, change)


async def publish_for_slot(
    session: AsyncSession, redis: Redis, topic: Topic, slot_id: uuid.UUID, *, change: str
) -> None:
    """Publish for the machine a slot belongs to."""
    machine_id = await session.scalar(
        select(MachineSlot.machine_id).where(MachineSlot.id == slot_id)
    )
    await publish(redis, topic, machine_id=machine_id, change=change)
