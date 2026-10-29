"""Live change notices for operators: the SSE stream, end to end over a real
server, and the notices the services publish."""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

import pytest
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import get_settings
from app.models import ReservationStatus, UserRole
from app.schemas.reservation import ReservationCreate
from app.services import events
from app.services.events import CHANNEL, Topic
from app.services.payments import MockPaymentProvider
from app.services.reservation_expiry import expire_due_reservations
from app.services.reservations import cancel_reservation, create_reservation
from tests.fake_machine import MakeMachine
from tests.sse_client import Notices, http_client, open_stream, user_with_token
from tests.world import World

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _stream(gateway_url: str, headers: dict[str, str]) -> AsyncIterator[Notices]:
    async with open_stream(gateway_url, "/api/v1/admin/events", headers) as notices:
        yield notices


async def test_stream_is_for_operators_only(world: World, gateway_url: str) -> None:
    _, customer = await user_with_token(world, UserRole.CUSTOMER)
    async with http_client(gateway_url) as http:
        assert (await http.get("/api/v1/admin/events")).status_code == 401
        assert (await http.get("/api/v1/admin/events", headers=customer)).status_code == 403
        forged = {"Authorization": "Bearer not.a.token"}
        assert (await http.get("/api/v1/admin/events", headers=forged)).status_code == 401


async def test_operator_sees_machines_stock_and_reservations_change(
    world: World, gateway_url: str, make_machine: MakeMachine, redis: Redis
) -> None:
    _, operator = await user_with_token(world, UserRole.OPERATOR)
    customer, _ = await user_with_token(world, UserRole.CUSTOMER)
    fake, machine, slots = await make_machine({"A1": 3})
    mid = str(machine.id)

    async with _stream(gateway_url, operator) as notices:
        await fake.connect()
        await notices.until("machine", "connected", mid)

        async with http_client(gateway_url) as http:
            await http.post("/api/v1/admin/stock-movements", headers=operator,
                            json={"slot_id": str(slots["A1"].id), "reason": "restock",
                                  "delta": 2})  # fmt: skip
        await notices.until("stock", "restock", mid)
        assert (await fake.recv())["type"] == "adjust_stock"  # the machine was told too

        async with world.sessions() as session:
            view, _ = await create_reservation(
                session, redis, customer, ReservationCreate(slot_id=slots["A1"].id)
            )
        await notices.until("reservation", "created", mid)

        await fake.request("telemetry", door="open", temperature_c=20)
        await notices.until("machine", "telemetry", mid)

        await fake.close()
        await notices.until("machine", "disconnected", mid)
    assert view.reservation.status is ReservationStatus.HELD


async def test_stream_ends_when_the_token_expires(
    world: World, gateway_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "access_token_ttl_seconds", 2)
    _, operator = await user_with_token(world, UserRole.OPERATOR)

    async with _stream(gateway_url, operator) as notices:
        await notices.until_closed(within=10)  # ~2 s, plus the JWT leeway


# --- what the services publish --------------------------------------------------


@asynccontextmanager
async def _listening(redis: Redis) -> AsyncIterator[list[dict[str, Any]]]:
    received: list[dict[str, Any]] = []
    pubsub = redis.pubsub()
    await pubsub.subscribe(CHANNEL)

    async def read() -> None:
        while True:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.1)
            if message is not None:
                received.append(json.loads(message["data"]))

    reader = asyncio.create_task(read())
    try:
        yield received
    finally:
        await asyncio.sleep(0.2)
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
        await pubsub.aclose()  # type: ignore[no-untyped-call]


async def test_reservation_lifecycle_publishes_notices(
    world: World, redis: Redis, provider: MockPaymentProvider
) -> None:
    from app.services.checkout import checkout

    slot = await world.slot(quantity=5)
    customer, _ = await user_with_token(world, UserRole.CUSTOMER)
    async with _listening(redis) as received:
        async with world.sessions() as session:
            first, _ = await create_reservation(
                session, redis, customer, ReservationCreate(slot_id=slot.id)
            )
            await checkout(session, redis, provider, customer, first.reservation.id, "pm_card_ok")
            await cancel_reservation(session, redis, provider, customer, first.reservation.id)
            second, _ = await create_reservation(
                session, redis, customer, ReservationCreate(slot_id=slot.id)
            )
            second.reservation.expires_at -= timedelta(hours=1)
            await session.commit()
            await expire_due_reservations(session, redis)
    mid = str(slot.machine_id)
    ours = [n["change"] for n in received if (n["topic"], n["machine_id"]) == ("reservation", mid)]
    assert ours == ["created", "paid", "cancelled", "created"]
    # The sweeper covers several machines at once, so its notice names none.
    assert any((n["topic"], n["change"], n["machine_id"]) == ("reservation", "expired", None)
               for n in received)  # fmt: skip


async def test_publishing_never_fails_the_operation() -> None:
    class Broken:
        async def publish(self, *_: Any) -> int:
            raise RedisError("down")

    await events.publish(Broken(), Topic.STOCK, machine_id=None, change="restock")  # type: ignore[arg-type]
