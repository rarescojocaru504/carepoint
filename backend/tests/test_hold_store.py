"""The Redis hold store in isolation, against a real Redis."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from redis.asyncio import Redis

from app.services import hold_store
from app.services.hold_store import ExistingHold, SlotState

pytestmark = pytest.mark.integration


def _loader(state: SlotState, calls: list[uuid.UUID] | None = None) -> hold_store.SlotStateLoader:
    async def load(slot_id: uuid.UUID) -> SlotState:
        if calls is not None:
            calls.append(slot_id)
        return state

    return load


async def _reserve(
    redis: Redis, slot: uuid.UUID, units: int = 1, *, physical: int = 5, ttl: int = 900
) -> hold_store.ReserveResult:
    return await hold_store.reserve(
        redis, slot, uuid.uuid4(), units, ttl, _loader(SlotState(physical, []))
    )


async def test_reserve_until_sold_out(redis: Redis) -> None:
    slot = uuid.uuid4()
    results = [await _reserve(redis, slot, 2, physical=5) for _ in range(3)]
    assert [r.granted for r in results] == [True, True, False]
    assert results[2].available == 1
    assert (await _reserve(redis, slot, 1, physical=5)).granted  # the last unit
    snap = await hold_store.snapshot(redis, slot)
    assert (snap.physical, snap.held, snap.available) == (5, 5, 0)


async def test_expiry_is_computed_from_redis_clock(redis: Redis) -> None:
    before = datetime.now(UTC)
    result = await _reserve(redis, uuid.uuid4(), ttl=900)
    assert result.expires_at is not None
    assert before + timedelta(seconds=899) <= result.expires_at <= before + timedelta(seconds=902)


async def test_expired_holds_free_stock_without_any_worker(redis: Redis) -> None:
    """The core property: an expired hold cannot keep a unit locked."""
    slot = uuid.uuid4()
    assert (await _reserve(redis, slot, 1, physical=1, ttl=1)).granted
    assert not (await _reserve(redis, slot, 1, physical=1, ttl=1)).granted
    await asyncio.sleep(1.1)
    assert (await _reserve(redis, slot, 1, physical=1, ttl=1)).granted


async def test_reserve_is_idempotent_per_reservation_id(redis: Redis) -> None:
    slot, rid = uuid.uuid4(), uuid.uuid4()
    loader = _loader(SlotState(3, []))
    first = await hold_store.reserve(redis, slot, rid, 2, 900, loader)
    second = await hold_store.reserve(redis, slot, rid, 2, 900, loader)
    assert first.expires_at == second.expires_at
    assert (await hold_store.snapshot(redis, slot)).held == 2


async def test_release_returns_units_and_is_idempotent(redis: Redis) -> None:
    slot, rid = uuid.uuid4(), uuid.uuid4()
    await hold_store.reserve(redis, slot, rid, 3, 900, _loader(SlotState(3, [])))
    assert await hold_store.release(redis, slot, rid) == 3
    assert await hold_store.release(redis, slot, rid) == 0
    assert (await hold_store.snapshot(redis, slot)).available == 3


async def test_lazy_init_loads_db_state_once(redis: Redis) -> None:
    """Missing keys (first use, flushed Redis) are rebuilt from Postgres's view,
    including holds that Postgres knows about."""
    slot = uuid.uuid4()
    db_hold = ExistingHold(uuid.uuid4(), 2, datetime.now(UTC) + timedelta(minutes=5))
    calls: list[uuid.UUID] = []
    loader = _loader(SlotState(3, [db_hold]), calls)

    first = await hold_store.reserve(redis, slot, uuid.uuid4(), 1, 900, loader)
    assert first.granted
    refused = await hold_store.reserve(redis, slot, uuid.uuid4(), 1, 900, loader)
    assert not refused.granted  # 3 physical - 2 (DB hold) - 1
    assert calls == [slot]  # loaded once, then served from Redis

    await redis.flushdb()  # simulate losing Redis entirely
    calls.clear()
    again = await hold_store.reserve(redis, slot, uuid.uuid4(), 1, 900, loader)
    assert again.granted and calls == [slot]


async def test_init_never_clobbers_existing_state(redis: Redis) -> None:
    slot = uuid.uuid4()
    assert await hold_store.initialise(redis, slot, SlotState(5, []))
    await _reserve(redis, slot, 4)
    assert not await hold_store.initialise(redis, slot, SlotState(5, []))  # racing init
    assert (await hold_store.snapshot(redis, slot)).held == 4


async def test_init_skips_already_expired_db_holds(redis: Redis) -> None:
    slot = uuid.uuid4()
    stale = ExistingHold(uuid.uuid4(), 5, datetime.now(UTC) - timedelta(seconds=1))
    await hold_store.initialise(redis, slot, SlotState(5, [stale]))
    assert (await hold_store.snapshot(redis, slot)).available == 5


async def test_apply_stock_delta(redis: Redis) -> None:
    slot = uuid.uuid4()
    await hold_store.apply_stock_delta(redis, slot, 10)  # not initialised: no-op
    assert not (await hold_store.snapshot(redis, slot)).initialised
    await hold_store.initialise(redis, slot, SlotState(2, []))
    await hold_store.apply_stock_delta(redis, slot, 3)
    await hold_store.apply_stock_delta(redis, slot, -1)
    assert (await hold_store.snapshot(redis, slot)).physical == 4


async def test_keys_share_a_cluster_hash_tag() -> None:
    slot = uuid.uuid4()
    keys = hold_store.slot_keys(slot)
    assert {k.split("{")[1].split("}")[0] for k in keys} == {str(slot)}


@pytest.mark.parametrize("round_", range(10))
async def test_concurrent_reserves_never_oversell(redis: Redis, round_: int) -> None:
    """100 coroutines race for 5 units on one slot: exactly 5 win, every round."""
    slot = uuid.uuid4()
    await hold_store.initialise(redis, slot, SlotState(5, []))
    results = await asyncio.gather(*(_reserve(redis, slot, physical=5) for _ in range(100)))
    assert sum(r.granted for r in results) == 5
    snap = await hold_store.snapshot(redis, slot)
    assert (snap.held, snap.available) == (5, 0)


async def test_concurrent_mixed_quantities_never_oversell(redis: Redis) -> None:
    slot = uuid.uuid4()
    await hold_store.initialise(redis, slot, SlotState(10, []))
    sizes = [1, 2, 3, 4] * 15
    results = await asyncio.gather(*(_reserve(redis, slot, n, physical=10) for n in sizes))
    granted = sum(n for n, r in zip(sizes, results, strict=True) if r.granted)
    assert granted <= 10
    assert (await hold_store.snapshot(redis, slot)).held == granted
    # Whatever is left is too small for every refused request.
    leftover = 10 - granted
    # Availability only shrinks here, so each refusal was for more than what is left.
    assert all(n > leftover for n, r in zip(sizes, results, strict=True) if not r.granted)
