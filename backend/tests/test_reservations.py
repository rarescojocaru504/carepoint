"""Reservations API: behaviour, limits, idempotency, and races under real concurrency."""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import create_access_token
from app.main import app
from app.models import (
    MachineSlot,
    MachineStatus,
    Product,
    Reservation,
    ReservationStatus,
    User,
)
from app.services import hold_store
from tests.factories import make_machine, make_product, make_reservation, make_slot, make_user
from tests.world import World

pytestmark = pytest.mark.integration


def _auth(user: User) -> dict[str, str]:
    token, _ = create_access_token(user.id, user.role)
    return {"Authorization": f"Bearer {token}"}


async def _reserve(
    api: httpx.AsyncClient, user: User, slot_id: uuid.UUID, **body: Any
) -> httpx.Response:
    return await api.post(
        "/api/v1/reservations", json={"slot_id": str(slot_id), **body}, headers=_auth(user)
    )


async def _stocked_slot(
    session: AsyncSession, quantity: int = 5, **machine: Any
) -> tuple[MachineSlot, Product]:
    product = await make_product(session, price_cents=1299)
    m = await make_machine(session, lat=10.0, lng=-30.0, **machine)
    slot = await make_slot(session, machine=m, product=product, quantity=quantity, capacity=10)
    return slot, product


async def _available(api: httpx.AsyncClient, slot: MachineSlot) -> int:
    detail = (await api.get(f"/api/v1/machines/{slot.machine_id}")).json()
    return int(next(s["available"] for s in detail["slots"] if s["slot_id"] == str(slot.id)))


# --- create ---------------------------------------------------------------------


async def test_create_reservation(
    api: httpx.AsyncClient, db_session: AsyncSession, redis: Redis
) -> None:
    user = await make_user(db_session)
    slot, product = await _stocked_slot(db_session, quantity=5)
    before = datetime.now(UTC)

    response = await _reserve(api, user, slot.id, quantity=2)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "held"
    assert (body["quantity"], body["unit_price_cents"], body["total_cents"]) == (2, 1299, 2598)
    assert body["product_sku"] == product.sku
    assert body["machine_id"] == str(slot.machine_id)
    expires = datetime.fromisoformat(body["expires_at"])
    hold = get_settings().reservation_hold_seconds
    assert before + timedelta(seconds=hold - 2) <= expires <= before + timedelta(seconds=hold + 2)
    # Both stores agree on the hold.
    assert (await hold_store.snapshot(redis, slot.id)).held == 2
    assert await _available(api, slot) == 3


async def test_price_is_snapshotted(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    slot, product = await _stocked_slot(db_session)
    rid = (await _reserve(api, user, slot.id)).json()["id"]
    product.price_cents = 9999
    await db_session.flush()
    got = (await api.get(f"/api/v1/reservations/{rid}", headers=_auth(user))).json()
    assert got["unit_price_cents"] == 1299


async def test_insufficient_stock(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    slot, _ = await _stocked_slot(db_session, quantity=2)
    response = await _reserve(api, user, slot.id, quantity=3)
    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "insufficient_stock",
        "message": "Not enough stock",
        "available": 2,
    }
    assert await db_session.scalar(select(func.count()).select_from(Reservation)) == 0


async def test_existing_db_holds_are_respected(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """Holds created before Redis knew the slot are loaded on first use."""
    other = await make_user(db_session)
    user = await make_user(db_session)
    slot, _ = await _stocked_slot(db_session, quantity=3)
    await make_reservation(db_session, user=other, slot=slot, quantity=2)
    assert (await _reserve(api, user, slot.id, quantity=2)).status_code == 409
    assert (await _reserve(api, user, slot.id, quantity=1)).status_code == 201


@pytest.mark.parametrize(
    "case", ["unknown", "decommissioned", "maintenance", "inactive-product", "empty-slot"]
)
async def test_unreservable_slots(
    api: httpx.AsyncClient, db_session: AsyncSession, case: str
) -> None:
    user = await make_user(db_session)
    if case == "unknown":
        slot_id, expected = uuid.uuid4(), 404
    elif case == "empty-slot":
        m = await make_machine(db_session)
        slot_id, expected = (await make_slot(db_session, machine=m, product=None)).id, 409
    else:
        statuses = {
            "decommissioned": MachineStatus.DECOMMISSIONED,
            "maintenance": MachineStatus.MAINTENANCE,
        }
        status = statuses.get(case, MachineStatus.ACTIVE)
        slot, product = await _stocked_slot(db_session, status=status)
        if case == "inactive-product":
            product.is_active = False
            await db_session.flush()
        slot_id = slot.id
        expected = 404 if case == "decommissioned" else 409
    assert (await _reserve(api, user, slot_id)).status_code == expected


@pytest.mark.parametrize(
    "body",
    [{"quantity": 0}, {"quantity": 11}, {"quantity": "1"}, {"quantity": 1.5}, {"extra": 1},
     {"idempotency_key": "short"}],
)  # fmt: skip
async def test_validation(
    api: httpx.AsyncClient, db_session: AsyncSession, body: dict[str, Any]
) -> None:
    user = await make_user(db_session)
    slot, _ = await _stocked_slot(db_session)
    assert (await _reserve(api, user, slot.id, **body)).status_code == 422


async def test_requires_authentication(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    slot, _ = await _stocked_slot(db_session)
    response = await api.post("/api/v1/reservations", json={"slot_id": str(slot.id)})
    assert response.status_code == 401


async def test_hold_length_follows_setting(
    api: httpx.AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "reservation_hold_seconds", 60)  # demo mode
    user = await make_user(db_session)
    slot, _ = await _stocked_slot(db_session)
    before = datetime.now(UTC)
    expires = datetime.fromisoformat((await _reserve(api, user, slot.id)).json()["expires_at"])
    assert before + timedelta(seconds=58) <= expires <= before + timedelta(seconds=62)


# --- limits & idempotency -------------------------------------------------------


async def test_active_reservation_limit(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    slot, _ = await _stocked_slot(db_session, quantity=10)
    limit = get_settings().max_active_reservations_per_user
    ids = [(await _reserve(api, user, slot.id)).json()["id"] for _ in range(limit)]
    over = await _reserve(api, user, slot.id)
    assert over.status_code == 409
    assert over.json()["detail"]["code"] == "too_many_active"
    # Cancelling frees a place.
    await api.delete(f"/api/v1/reservations/{ids[0]}", headers=_auth(user))
    assert (await _reserve(api, user, slot.id)).status_code == 201


async def test_expired_holds_do_not_count_towards_limit(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    slot, _ = await _stocked_slot(db_session, quantity=10)
    for _ in range(get_settings().max_active_reservations_per_user):
        await make_reservation(db_session, user=user, slot=slot, expires_in=timedelta(seconds=-1))
    assert (await _reserve(api, user, slot.id)).status_code == 201


async def test_idempotent_retry_returns_same_reservation(
    api: httpx.AsyncClient, db_session: AsyncSession, redis: Redis
) -> None:
    user = await make_user(db_session)
    slot, _ = await _stocked_slot(db_session, quantity=5)
    first = await _reserve(api, user, slot.id, quantity=2, idempotency_key="retry-key-0001")
    again = await _reserve(api, user, slot.id, quantity=2, idempotency_key="retry-key-0001")
    assert (first.status_code, again.status_code) == (201, 200)
    assert first.json()["id"] == again.json()["id"]
    assert (await hold_store.snapshot(redis, slot.id)).held == 2  # held once, not twice


async def test_idempotency_key_reuse_with_other_params(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    slot, _ = await _stocked_slot(db_session)
    await _reserve(api, user, slot.id, quantity=1, idempotency_key="retry-key-0002")
    clash = await _reserve(api, user, slot.id, quantity=2, idempotency_key="retry-key-0002")
    assert clash.status_code == 409
    assert clash.json()["detail"]["code"] == "idempotency_conflict"


async def test_idempotency_keys_are_per_user(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    alice, bob = await make_user(db_session), await make_user(db_session)
    slot, _ = await _stocked_slot(db_session)
    a = await _reserve(api, alice, slot.id, idempotency_key="shared-key-001")
    b = await _reserve(api, bob, slot.id, idempotency_key="shared-key-001")
    assert (a.status_code, b.status_code) == (201, 201)
    assert a.json()["id"] != b.json()["id"]


async def test_create_rate_limited_per_user(
    api: httpx.AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "rate_limit_reservation_user", "2/60")
    user = await make_user(db_session)
    slot, _ = await _stocked_slot(db_session)
    statuses = [(await _reserve(api, user, slot.id, quantity=11)).status_code for _ in range(3)]
    assert statuses == [422, 422, 429]  # counted before validation of the body


# --- read & cancel --------------------------------------------------------------


async def test_list_and_get_own_only(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    alice, bob = await make_user(db_session), await make_user(db_session)
    slot, _ = await _stocked_slot(db_session)
    rid = (await _reserve(api, alice, slot.id)).json()["id"]
    mine = (await api.get("/api/v1/reservations", headers=_auth(alice))).json()
    assert [r["id"] for r in mine] == [rid]
    assert (await api.get("/api/v1/reservations", headers=_auth(bob))).json() == []
    assert (await api.get(f"/api/v1/reservations/{rid}", headers=_auth(bob))).status_code == 404


async def test_expired_hold_reads_as_expired(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    slot, _ = await _stocked_slot(db_session)
    r = await make_reservation(db_session, user=user, slot=slot, expires_in=timedelta(seconds=-1))
    body = (await api.get(f"/api/v1/reservations/{r.id}", headers=_auth(user))).json()
    assert body["status"] == "expired"  # before any sweeper ran


async def test_cancel_releases_stock(
    api: httpx.AsyncClient, db_session: AsyncSession, redis: Redis
) -> None:
    user = await make_user(db_session)
    slot, _ = await _stocked_slot(db_session, quantity=1)
    rid = (await _reserve(api, user, slot.id)).json()["id"]
    assert await _available(api, slot) == 0

    assert (await api.delete(f"/api/v1/reservations/{rid}", headers=_auth(user))).status_code == 204
    assert await _available(api, slot) == 1
    assert (await hold_store.snapshot(redis, slot.id)).held == 0
    got = (await api.get(f"/api/v1/reservations/{rid}", headers=_auth(user))).json()
    assert got["status"] == "cancelled"
    # Idempotent.
    assert (await api.delete(f"/api/v1/reservations/{rid}", headers=_auth(user))).status_code == 204


async def test_cancel_someone_elses_or_unknown(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    alice, bob = await make_user(db_session), await make_user(db_session)
    slot, _ = await _stocked_slot(db_session)
    rid = (await _reserve(api, alice, slot.id)).json()["id"]
    assert (await api.delete(f"/api/v1/reservations/{rid}", headers=_auth(bob))).status_code == 404
    unknown = f"/api/v1/reservations/{uuid.uuid4()}"
    assert (await api.delete(unknown, headers=_auth(alice))).status_code == 404


@pytest.mark.parametrize(
    ("status", "expires_in"),
    [(ReservationStatus.DISPENSED, timedelta(minutes=5)),
     (ReservationStatus.EXPIRED, timedelta(minutes=-5)),
     (ReservationStatus.HELD, timedelta(seconds=-1))],
    ids=["dispensed", "expired", "lapsed-not-swept"],
)  # fmt: skip
async def test_cannot_cancel_finished_reservations(
    api: httpx.AsyncClient,
    db_session: AsyncSession,
    status: ReservationStatus,
    expires_in: timedelta,
) -> None:
    user = await make_user(db_session)
    slot, _ = await _stocked_slot(db_session)
    r = await make_reservation(
        db_session, user=user, slot=slot, status=status, expires_in=expires_in
    )
    response = await api.delete(f"/api/v1/reservations/{r.id}", headers=_auth(user))
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "not_cancellable"


async def test_failed_db_insert_releases_redis_hold(
    api: httpx.AsyncClient, db_session: AsyncSession, redis: Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If Postgres rejects the row after Redis granted the hold, the hold is returned."""
    user = await make_user(db_session)
    slot, _ = await _stocked_slot(db_session, quantity=1)

    async def failing_commit() -> None:
        raise RuntimeError("database went away")

    monkeypatch.setattr(db_session, "commit", failing_commit)
    with pytest.raises(RuntimeError):
        await _reserve(api, user, slot.id)
    snap = await hold_store.snapshot(redis, slot.id)
    assert (snap.held, snap.available) == (0, 1)


# --- races under real concurrency ----------------------------------------------
#
# These use the committed `world` fixture (tests/world.py): one database
# connection per request, like production.


async def _held_count(world: World, slot: MachineSlot) -> int:
    async with world.sessions() as session:
        return int(
            await session.scalar(
                select(func.coalesce(func.sum(Reservation.quantity), 0)).where(
                    Reservation.slot_id == slot.id, Reservation.status == ReservationStatus.HELD
                )
            )
            or 0
        )


async def _gather(*calls: Callable[[], Awaitable[httpx.Response]]) -> list[int]:
    return [r.status_code for r in await asyncio.gather(*(c() for c in calls))]


@pytest.mark.parametrize("round_", range(20))
async def test_race_for_the_last_unit(world: World, redis: Redis, round_: int) -> None:
    """40 customers press "reserve" on the last unit at the same moment."""
    slot = await world.slot(quantity=1)
    users = await world.users(40)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        statuses = await _gather(*(partial(_reserve, c, u, slot.id) for u in users))
    assert statuses.count(201) == 1
    assert statuses.count(409) == 39
    assert await _held_count(world, slot) == 1
    assert (await hold_store.snapshot(redis, slot.id)).held == 1


async def test_race_many_units_many_customers(world: World, redis: Redis) -> None:
    slot = await world.slot(quantity=7)
    users = await world.users(30)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        statuses = await _gather(*(partial(_reserve, c, u, slot.id) for u in users))
    assert statuses.count(201) == 7
    assert await _held_count(world, slot) == 7
    snap = await hold_store.snapshot(redis, slot.id)
    assert (snap.held, snap.available) == (7, 0)


async def test_race_same_user_double_submit(world: World, redis: Redis) -> None:
    """A double-tapped button (same idempotency key) creates one reservation."""
    slot = await world.slot(quantity=5)
    (user,) = await world.users(1)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        responses = await asyncio.gather(
            *(_reserve(c, user, slot.id, idempotency_key="double-tap-01") for _ in range(8))
        )
    assert {r.status_code for r in responses} <= {200, 201}
    assert len({r.json()["id"] for r in responses}) == 1
    assert await _held_count(world, slot) == 1
    assert (await hold_store.snapshot(redis, slot.id)).held == 1


async def test_race_same_user_cannot_exceed_limit(world: World) -> None:
    slot = await world.slot(quantity=10)
    (user,) = await world.users(1)
    limit = get_settings().max_active_reservations_per_user
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        statuses = await _gather(*(partial(_reserve, c, user, slot.id) for _ in range(10)))
    assert statuses.count(201) == limit
    assert await _held_count(world, slot) == limit


async def test_race_cancel_and_reserve(world: World, redis: Redis) -> None:
    """Cancels and new reservations interleaving never oversell or lose a unit."""
    slot = await world.slot(quantity=3)
    holders = await world.users(3)
    newcomers = await world.users(20)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        ids = [(await _reserve(c, u, slot.id)).json()["id"] for u in holders]
        calls: list[Callable[[], Awaitable[httpx.Response]]] = [
            partial(c.delete, f"/api/v1/reservations/{rid}", headers=_auth(u))
            for u, rid in zip(holders, ids, strict=True)
        ]
        calls += [partial(_reserve, c, u, slot.id) for u in newcomers]
        await _gather(*calls)
    held = await _held_count(world, slot)
    snap = await hold_store.snapshot(redis, slot.id)
    assert held <= 3
    assert snap.held == held  # Redis and Postgres agree


async def test_fast_fail_matches_full_path(
    api: httpx.AsyncClient, db_session: AsyncSession, redis: Redis
) -> None:
    """Once Redis knows a slot is short, refusals come from the fast path with the
    same response as the full path, and nothing is written."""
    alice, bob = await make_user(db_session), await make_user(db_session)
    slot, _ = await _stocked_slot(db_session, quantity=1)
    assert (await _reserve(api, alice, slot.id)).status_code == 201
    refused = await _reserve(api, bob, slot.id)
    assert refused.status_code == 409
    assert refused.json()["detail"] == {
        "code": "insufficient_stock",
        "message": "Not enough stock",
        "available": 0,
    }
    assert await db_session.scalar(select(func.count()).select_from(Reservation)) == 1


async def test_idempotent_retry_after_sell_out_returns_original(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """A retried request whose reservation exists must not be fast-failed as sold out."""
    user = await make_user(db_session)
    slot, _ = await _stocked_slot(db_session, quantity=1)
    first = await _reserve(api, user, slot.id, idempotency_key="retry-after-sellout")
    retry = await _reserve(api, user, slot.id, idempotency_key="retry-after-sellout")
    assert (first.status_code, retry.status_code) == (201, 200)
    assert first.json()["id"] == retry.json()["id"]
