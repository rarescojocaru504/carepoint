"""Live change notices for operator dashboards (ADR-022) and the customer app (ADR-026).

Services publish a small notice after they commit a change: which kind of
thing changed (`topic`) and, where known, at which machine. Notices carry no
data: a dashboard or the app re-reads what it shows through the normal API, so
it can never drift from it and access control stays in one place.

Two kinds of channel:
- `cp:events`: every notice, for operators. Customers get a filtered view of
  it (`customer_view`): which machines' availability or status changed, and
  nothing about who reserved what.
- `cp:user-events:<user id>`: notices about one user's own reservations,
  carrying the reservation id. Only that user's stream subscribes to it.

Publishing is best effort. A notice lost to a Redis hiccup costs one stale
view until the next notice or refresh, never a failed operation.
"""

import json
import logging
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MachineSlot, Reservation
from app.schemas.events import CustomerNotice

logger = logging.getLogger(__name__)

CHANNEL = "cp:events"


class Topic(StrEnum):
    MACHINE = "machine"  # connected, disconnected, telemetry, fault, status
    STOCK = "stock"  # a slot's physical count changed
    RESERVATION = "reservation"  # created, paid, cancelled, expired, dispensed


def user_channel(user_id: uuid.UUID) -> str:
    return f"cp:user-events:{user_id}"


async def _send(redis: Redis, channel: str, notice: dict[str, Any]) -> None:
    try:
        await redis.publish(channel, json.dumps(notice))
    except RedisError:
        logger.warning("Could not publish %s/%s notice", notice["topic"], notice["change"])


def _now() -> str:
    return datetime.now(UTC).isoformat()


async def publish(redis: Redis, topic: Topic, *, machine_id: uuid.UUID | None, change: str) -> None:
    """`machine_id` None means "possibly several machines": refresh them all."""
    notice = {
        "topic": topic.value,
        "machine_id": str(machine_id) if machine_id is not None else None,
        "change": change,
        "at": _now(),
    }
    await _send(redis, CHANNEL, notice)


async def _machine_of(session: AsyncSession, slot_id: uuid.UUID) -> uuid.UUID | None:
    machine_id: uuid.UUID | None = await session.scalar(
        select(MachineSlot.machine_id).where(MachineSlot.id == slot_id)
    )
    return machine_id


async def publish_for_slot(
    session: AsyncSession, redis: Redis, topic: Topic, slot_id: uuid.UUID, *, change: str
) -> None:
    """Publish for the machine a slot belongs to."""
    await publish(redis, topic, machine_id=await _machine_of(session, slot_id), change=change)


async def publish_to_owner(
    redis: Redis,
    *,
    user_id: uuid.UUID,
    reservation_id: uuid.UUID,
    machine_id: uuid.UUID | None,
    change: str,
) -> None:
    """Tell one customer that their reservation changed (their channel only)."""
    notice = {
        "topic": Topic.RESERVATION.value,
        "machine_id": str(machine_id) if machine_id is not None else None,
        "reservation_id": str(reservation_id),
        "change": change,
        "at": _now(),
    }
    await _send(redis, user_channel(user_id), notice)


async def publish_reservation(
    redis: Redis, reservation: Reservation, *, machine_id: uuid.UUID | None, change: str
) -> None:
    """A reservation changed: tell operators (all reservations) and its owner."""
    await publish(redis, Topic.RESERVATION, machine_id=machine_id, change=change)
    await publish_to_owner(
        redis,
        user_id=reservation.user_id,
        reservation_id=reservation.id,
        machine_id=machine_id,
        change=change,
    )


async def publish_reservation_for_slot(
    session: AsyncSession, redis: Redis, reservation: Reservation, *, change: str
) -> None:
    machine_id = await _machine_of(session, reservation.slot_id)
    await publish_reservation(redis, reservation, machine_id=machine_id, change=change)


# --- what customers see of the shared channel ---------------------------------------

# Reservation changes that move a machine's availability (stock minus holds).
# Dispensed and partial also publish a stock notice, so they arrive that way.
_AVAILABILITY_CHANGES = {"created", "cancelled", "expired"}
_MACHINE_CHANGES = {"connected": "online", "disconnected": "offline", "status": "status"}


def customer_view(raw: str | bytes) -> CustomerNotice | None:
    """The customer-safe form of an operator notice, or None to drop it.

    Customers learn *that* a machine's availability or status changed, which
    they could also see by re-reading the public discovery API (ADR-011), but
    never whose reservation it was, nor telemetry or faults.
    """
    try:
        notice = json.loads(raw)
        topic, change = notice["topic"], notice["change"]
        machine_id = uuid.UUID(notice["machine_id"]) if notice["machine_id"] else None
        at = datetime.fromisoformat(notice["at"])
    except (ValueError, TypeError, KeyError):
        return None
    if topic == Topic.STOCK or (topic == Topic.RESERVATION and change in _AVAILABILITY_CHANGES):
        return CustomerNotice(topic="stock", machine_id=machine_id, change="availability", at=at)
    if topic == Topic.MACHINE and change in _MACHINE_CHANGES:
        return CustomerNotice(
            topic="machine", machine_id=machine_id, change=_MACHINE_CHANGES[change], at=at
        )
    return None


def owner_view(raw: str | bytes) -> CustomerNotice | None:
    """A notice from the user's own channel, validated before it is passed on."""
    try:
        return CustomerNotice.model_validate_json(raw)
    except ValueError:
        return None


# --- how many streams one user may hold open ----------------------------------------

# INCR with a safety TTL in one step; refuse (and undo) above the limit. The TTL
# outlives any stream (streams end at token expiry), so a worker that dies
# without releasing can't lock a user out for longer than that.
_ACQUIRE = """
local n = redis.call('INCR', KEYS[1])
redis.call('EXPIRE', KEYS[1], ARGV[2])
if n > tonumber(ARGV[1]) then
  redis.call('DECR', KEYS[1])
  return 0
end
return 1
"""


def _streams_key(user_id: uuid.UUID) -> str:
    return f"cp:event-streams:{user_id}"


async def acquire_stream(redis: Redis, user_id: uuid.UUID, *, limit: int, ttl: int) -> bool:
    """Count one more open stream for the user; False if they're at the limit."""
    allowed = await redis.eval(  # type: ignore[misc]
        _ACQUIRE, 1, _streams_key(user_id), str(limit), str(ttl)
    )
    return int(allowed) == 1


async def release_stream(redis: Redis, user_id: uuid.UUID) -> None:
    try:
        await redis.decr(_streams_key(user_id))
    except RedisError:
        pass  # the key's TTL tidies up
