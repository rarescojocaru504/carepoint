"""Redis hold store: the atomic gate that prevents overselling (ADR-012).

Per slot, under one hash tag so a Redis Cluster keeps them on one shard:

    cp:slot:{<slot_id>}:qty      STRING  physical units in the slot (mirror of Postgres)
    cp:slot:{<slot_id>}:holds    ZSET    reservation_id -> expiry, epoch milliseconds
    cp:slot:{<slot_id>}:holdqty  HASH    reservation_id -> units held

Expiry is part of the arithmetic, not a side effect: every script first drops
holds whose expiry is in the past (by Redis's own clock), so an expired hold
can never keep a unit locked, and no background job, keyspace notification or
TTL callback is needed for correctness. Each operation is one Lua script, so
check-and-write is atomic: concurrent requests for the last unit serialise
inside Redis and exactly one succeeds.

Postgres stays the source of truth. If the slot's keys are missing (first use,
Redis flushed or restarted without persistence) the reserve script reports
NEEDS_INIT and the caller rebuilds them from the database before retrying.
"""

import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis

# Shared Lua prelude: current Redis time in ms, and a purge of expired holds.
_PRELUDE = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local expired = redis.call('ZRANGEBYSCORE', KEYS[2], '-inf', now)
for _, id in ipairs(expired) do redis.call('HDEL', KEYS[3], id) end
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now)
local function held_units()
    local total = 0
    for _, v in ipairs(redis.call('HVALS', KEYS[3])) do total = total + tonumber(v) end
    return total
end
"""

# ARGV: reservation_id, units, ttl_ms
# Returns {status, value}: status 1 = held (value = expiry ms),
#   0 = insufficient (value = units available), -1 = needs init.
_RESERVE = (
    _PRELUDE
    + """
local qty = redis.call('GET', KEYS[1])
if not qty then return {-1, 0} end
local existing = redis.call('ZSCORE', KEYS[2], ARGV[1])
if existing then return {1, tonumber(existing)} end
local available = tonumber(qty) - held_units()
if available < tonumber(ARGV[2]) then return {0, math.max(available, 0)} end
local expires = now + tonumber(ARGV[3])
redis.call('ZADD', KEYS[2], expires, ARGV[1])
redis.call('HSET', KEYS[3], ARGV[1], ARGV[2])
return {1, expires}
"""
)

# ARGV: reservation_id. Returns units released (0 if it was not held).
_RELEASE = """
local units = redis.call('HGET', KEYS[3], ARGV[1])
redis.call('ZREM', KEYS[2], ARGV[1])
redis.call('HDEL', KEYS[3], ARGV[1])
return tonumber(units) or 0
"""

# ARGV: physical qty, then (reservation_id, units, expiry_ms) triples.
# Only initialises if the slot has no state, so a concurrent init cannot clobber
# holds granted in the meantime. Returns 1 if it initialised, 0 if already set.
_INIT = """
if redis.call('EXISTS', KEYS[1]) == 1 then return 0 end
redis.call('SET', KEYS[1], ARGV[1])
for i = 2, #ARGV, 3 do
    redis.call('ZADD', KEYS[2], ARGV[i + 2], ARGV[i])
    redis.call('HSET', KEYS[3], ARGV[i], ARGV[i + 1])
end
return 1
"""

# ARGV: delta. Applies a committed stock change; no-op if the slot is not
# initialised (the next init will read the new quantity from Postgres).
_APPLY_DELTA = """
if redis.call('EXISTS', KEYS[1]) == 0 then return nil end
return redis.call('INCRBY', KEYS[1], ARGV[1])
"""

# Returns {physical qty or -1, units held, units available}.
_SNAPSHOT = (
    _PRELUDE
    + """
local qty = redis.call('GET', KEYS[1])
if not qty then return {-1, 0, 0} end
local held = held_units()
return {tonumber(qty), held, math.max(tonumber(qty) - held, 0)}
"""
)

# Returns {qty or -1, id1, units1, expiry1, id2, ...} after purging expired holds.
_STATE = (
    _PRELUDE
    + """
local qty = redis.call('GET', KEYS[1])
local out = {tonumber(qty) or -1}
local entries = redis.call('ZRANGE', KEYS[2], 0, -1, 'WITHSCORES')
for i = 1, #entries, 2 do
    out[#out + 1] = entries[i]
    out[#out + 1] = tonumber(redis.call('HGET', KEYS[3], entries[i])) or 0
    out[#out + 1] = tonumber(entries[i + 1])
end
return out
"""
)

# ARGV: expected_qty, new_qty, n_remove, remove ids..., then (id, units, expiry) triples.
# Applies a diff, so holds granted concurrently are never dropped. The quantity
# is compare-and-set: if it changed since the caller read it (a dispense or
# restock landed meanwhile), it is left alone and fixed on the next pass.
# Returns {removed, added, qty_updated}.
_RECONCILE = """
local removed, added = 0, 0
local n_remove = tonumber(ARGV[3])
for i = 4, 3 + n_remove do
    removed = removed + redis.call('ZREM', KEYS[2], ARGV[i])
    redis.call('HDEL', KEYS[3], ARGV[i])
end
for i = 4 + n_remove, #ARGV, 3 do
    if redis.call('ZADD', KEYS[2], 'NX', ARGV[i + 2], ARGV[i]) == 1 then
        redis.call('HSET', KEYS[3], ARGV[i], ARGV[i + 1])
        added = added + 1
    end
end
local qty_updated = 0
if ARGV[1] ~= ARGV[2] and redis.call('GET', KEYS[1]) == ARGV[1] then
    redis.call('SET', KEYS[1], ARGV[2])
    qty_updated = 1
end
return {removed, added, qty_updated}
"""

# ARGV: reservation_id, units. A committed dispense: drop the hold and the
# physical units together, so availability never dips or spikes in between.
_DISPENSE = """
redis.call('ZREM', KEYS[2], ARGV[1])
redis.call('HDEL', KEYS[3], ARGV[1])
if redis.call('EXISTS', KEYS[1]) == 1 then
    return redis.call('DECRBY', KEYS[1], ARGV[2])
end
return nil
"""


@dataclass(frozen=True)
class ExistingHold:
    reservation_id: uuid.UUID
    units: int
    expires_at: datetime


@dataclass(frozen=True)
class SlotState:
    """What Postgres says about a slot, for (re)building its Redis state."""

    physical_quantity: int
    holds: Sequence[ExistingHold]


@dataclass(frozen=True)
class ReserveResult:
    granted: bool
    expires_at: datetime | None  # set when granted
    available: int  # units available when refused


@dataclass(frozen=True)
class SlotSnapshot:
    initialised: bool
    physical: int
    held: int
    available: int


SlotStateLoader = Callable[[uuid.UUID], Awaitable[SlotState]]


def slot_keys(slot_id: uuid.UUID) -> list[str]:
    tag = f"{{{slot_id}}}"
    return [f"cp:slot:{tag}:qty", f"cp:slot:{tag}:holds", f"cp:slot:{tag}:holdqty"]


def _ms_to_datetime(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, UTC)


def _datetime_to_ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


async def _eval(redis: Redis, script: str, slot_id: uuid.UUID, *args: str | int) -> Any:
    # redis-py types script results as Any; callers convert explicitly.
    return await redis.register_script(script)(keys=slot_keys(slot_id), args=list(args))


async def initialise(redis: Redis, slot_id: uuid.UUID, state: SlotState) -> bool:
    args: list[str | int] = [state.physical_quantity]
    for hold in state.holds:
        args += [str(hold.reservation_id), hold.units, _datetime_to_ms(hold.expires_at)]
    return bool(await _eval(redis, _INIT, slot_id, *args))


async def reserve(
    redis: Redis,
    slot_id: uuid.UUID,
    reservation_id: uuid.UUID,
    units: int,
    ttl_seconds: int,
    load_state: SlotStateLoader,
) -> ReserveResult:
    """Atomically hold `units` of a slot, or report how many are available.

    Idempotent per reservation_id: repeating the call returns the same hold.
    """
    for _attempt in range(2):
        result = await _eval(
            redis, _RESERVE, slot_id, str(reservation_id), units, ttl_seconds * 1000
        )
        status, value = (int(x) for x in result)
        if status == 1:
            return ReserveResult(granted=True, expires_at=_ms_to_datetime(value), available=0)
        if status == 0:
            return ReserveResult(granted=False, expires_at=None, available=value)
        await initialise(redis, slot_id, await load_state(slot_id))
    raise RuntimeError(f"hold store for slot {slot_id} did not initialise")


async def release(redis: Redis, slot_id: uuid.UUID, reservation_id: uuid.UUID) -> int:
    """Drop a hold (cancel, expiry, failed insert). Returns units released."""
    return int(await _eval(redis, _RELEASE, slot_id, str(reservation_id)))


async def apply_stock_delta(redis: Redis, slot_id: uuid.UUID, delta: int) -> None:
    """Mirror a *committed* change of physical stock (restock, adjustment)."""
    await _eval(redis, _APPLY_DELTA, slot_id, delta)


async def snapshot(redis: Redis, slot_id: uuid.UUID) -> SlotSnapshot:
    physical, held, available = (int(x) for x in await _eval(redis, _SNAPSHOT, slot_id))
    return SlotSnapshot(physical >= 0, max(physical, 0), held, available)


@dataclass(frozen=True)
class SlotHolds:
    """Redis's full view of a slot (expired holds already purged)."""

    initialised: bool
    physical: int
    holds: dict[uuid.UUID, ExistingHold]


@dataclass(frozen=True)
class ReconcileResult:
    removed: int
    added: int
    quantity_updated: bool


async def read_state(redis: Redis, slot_id: uuid.UUID) -> SlotHolds:
    raw = await _eval(redis, _STATE, slot_id)
    holds: dict[uuid.UUID, ExistingHold] = {}
    for i in range(1, len(raw), 3):
        rid = uuid.UUID(raw[i].decode() if isinstance(raw[i], bytes) else raw[i])
        holds[rid] = ExistingHold(rid, int(raw[i + 1]), _ms_to_datetime(int(raw[i + 2])))
    physical = int(raw[0])
    return SlotHolds(physical >= 0, max(physical, 0), holds)


async def apply_reconciliation(
    redis: Redis,
    slot_id: uuid.UUID,
    *,
    expected_quantity: int,
    new_quantity: int,
    remove: Sequence[uuid.UUID],
    add: Sequence[ExistingHold],
) -> ReconcileResult:
    args: list[str | int] = [expected_quantity, new_quantity, len(remove)]
    args += [str(rid) for rid in remove]
    for hold in add:
        args += [str(hold.reservation_id), hold.units, _datetime_to_ms(hold.expires_at)]
    removed, added, updated = (int(x) for x in await _eval(redis, _RECONCILE, slot_id, *args))
    return ReconcileResult(removed, added, bool(updated))


async def complete_dispense(
    redis: Redis, slot_id: uuid.UUID, reservation_id: uuid.UUID, units: int
) -> None:
    """Mirror a *committed* dispense: release the hold and the physical units."""
    await _eval(redis, _DISPENSE, slot_id, str(reservation_id), units)
