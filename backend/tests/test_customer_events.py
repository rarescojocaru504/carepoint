"""Live notices for the customer app (ADR-026): the stream over a real server,
its privacy rule, the per-user stream limit, and what the services publish
to each reservation's owner."""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import get_settings
from app.models import UserRole
from app.schemas.reservation import ReservationCreate
from app.services import events
from app.services.checkout import checkout
from app.services.events import customer_view, owner_view
from app.services.payments import MockPaymentProvider
from app.services.reservation_expiry import expire_due_reservations
from app.services.reservations import cancel_reservation, create_reservation
from tests.fake_machine import MakeMachine
from tests.sse_client import Notices, http_client, open_stream, user_with_token
from tests.world import World

pytestmark = pytest.mark.integration

PATH = "/api/v1/events"
NOTICE_KEYS = {"topic", "machine_id", "reservation_id", "change", "at"}


@asynccontextmanager
async def _stream(gateway_url: str, headers: dict[str, str]) -> AsyncIterator[Notices]:
    async with open_stream(gateway_url, PATH, headers) as notices:
        yield notices


def _reservation_notices(notices: Notices) -> list[dict[str, Any]]:
    return [n for n in notices.seen if n["topic"] == "reservation"]


# --- access -----------------------------------------------------------------------


async def test_stream_needs_a_valid_token(world: World, gateway_url: str) -> None:
    async with http_client(gateway_url) as http:
        assert (await http.get(PATH)).status_code == 401
        forged = {"Authorization": "Bearer not.a.token"}
        assert (await http.get(PATH, headers=forged)).status_code == 401


async def test_any_signed_in_user_may_listen(world: World, gateway_url: str) -> None:
    for role in (UserRole.CUSTOMER, UserRole.OPERATOR):
        _, headers = await user_with_token(world, role)
        async with _stream(gateway_url, headers) as notices:
            hello = notices.seen[0]
            assert set(hello) == NOTICE_KEYS
            assert (hello["topic"], hello["change"]) == ("hello", "subscribed")


# --- the privacy rule -----------------------------------------------------------------


async def test_customers_see_their_own_reservations_and_only_availability_of_others(
    world: World,
    gateway_url: str,
    make_machine: MakeMachine,
    redis: Redis,
    provider: MockPaymentProvider,
) -> None:
    alice, alice_headers = await user_with_token(world, UserRole.CUSTOMER)
    _, bob_headers = await user_with_token(world, UserRole.CUSTOMER)
    _, machine, slots = await make_machine({"A1": 3})
    mid = str(machine.id)

    async with (
        _stream(gateway_url, alice_headers) as mine,
        _stream(gateway_url, bob_headers) as theirs,
    ):
        async with world.sessions() as session:
            view, _ = await create_reservation(
                session, redis, alice, ReservationCreate(slot_id=slots["A1"].id)
            )
            rid = str(view.reservation.id)
            created = await mine.until("reservation", "created", mid)
            assert created["reservation_id"] == rid
            # The shared channel is published first, so this came just before.
            assert ("stock", "availability", mid) in [
                (n["topic"], n["change"], n["machine_id"]) for n in mine.seen
            ]
            await theirs.until("stock", "availability", mid)  # someone took a unit

            await checkout(session, redis, provider, alice, view.reservation.id, "pm_card_ok")
            paid = await mine.until("reservation", "paid", mid)
            assert paid["reservation_id"] == rid

            await cancel_reservation(session, redis, provider, alice, view.reservation.id)
            await mine.until("reservation", "cancelled", mid)
            await theirs.until("stock", "availability", mid)  # the unit is back

        # Bob learned that availability changed, never whose reservation it was.
        assert _reservation_notices(theirs) == []
        for notice in theirs.seen:
            assert set(notice) == NOTICE_KEYS
            assert notice["reservation_id"] is None
        assert {n["reservation_id"] for n in _reservation_notices(mine)} == {rid}


async def test_customers_see_machines_go_online_and_offline_but_not_telemetry(
    world: World, gateway_url: str, make_machine: MakeMachine
) -> None:
    _, headers = await user_with_token(world, UserRole.CUSTOMER)
    fake, machine, _ = await make_machine()
    mid = str(machine.id)

    async with _stream(gateway_url, headers) as notices:
        await fake.connect()
        await notices.until("machine", "online", mid)
        await fake.request("telemetry", door="open", temperature_c=20)
        await fake.close()
        await notices.until("machine", "offline", mid)
    ours = [n["change"] for n in notices.seen if n["machine_id"] == mid]
    assert "telemetry" not in ours and "connected" not in ours


async def test_maintenance_is_announced(
    world: World, gateway_url: str, make_machine: MakeMachine
) -> None:
    _, customer = await user_with_token(world, UserRole.CUSTOMER)
    _, operator = await user_with_token(world, UserRole.OPERATOR)
    _, machine, _ = await make_machine()
    mid = str(machine.id)

    async with _stream(gateway_url, customer) as notices, http_client(gateway_url) as http:
        response = await http.patch(
            f"/api/v1/admin/machines/{mid}", headers=operator, json={"status": "maintenance"}
        )
        assert response.status_code == 200
        await notices.until("machine", "status", mid)


async def test_owner_hears_about_expiry(world: World, gateway_url: str, redis: Redis) -> None:
    slot = await world.slot(quantity=2)
    owner, owner_headers = await user_with_token(world, UserRole.CUSTOMER)
    _, other_headers = await user_with_token(world, UserRole.CUSTOMER)

    async with (
        _stream(gateway_url, owner_headers) as mine,
        _stream(gateway_url, other_headers) as theirs,
    ):
        async with world.sessions() as session:
            view, _ = await create_reservation(
                session, redis, owner, ReservationCreate(slot_id=slot.id)
            )
            view.reservation.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()
            await expire_due_reservations(session, redis)
        expired = await mine.until("reservation", "expired", None)
        assert expired["reservation_id"] == str(view.reservation.id)
        await theirs.until("stock", "availability", None)  # "refresh every machine"
        assert _reservation_notices(theirs) == []


# --- the stream itself ------------------------------------------------------------------


async def test_one_user_may_hold_only_a_few_streams(
    world: World, gateway_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "customer_event_streams_per_user", 2)
    _, headers = await user_with_token(world, UserRole.CUSTOMER)
    _, someone_else = await user_with_token(world, UserRole.CUSTOMER)

    async with _stream(gateway_url, headers), _stream(gateway_url, headers):
        async with http_client(gateway_url) as http:
            refused = await http.get(PATH, headers=headers)
            assert refused.status_code == 429
        async with _stream(gateway_url, someone_else):
            pass  # the limit is per user

    # Closing streams gives their places back (the server notices within ~1 s).
    async def reopened() -> bool:
        async with (
            http_client(gateway_url) as http,
            http.stream("GET", PATH, headers=headers) as response,
        ):
            return response.status_code == 200

    for _ in range(30):
        if await reopened():
            return
        await asyncio.sleep(0.2)
    raise AssertionError("the closed streams were never released")


async def test_stream_ends_when_the_token_expires_and_frees_its_place(
    world: World, gateway_url: str, redis: Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "access_token_ttl_seconds", 2)
    user, headers = await user_with_token(world, UserRole.CUSTOMER)

    async with _stream(gateway_url, headers) as notices:
        await notices.until_closed(within=10)  # ~2 s, plus the JWT leeway
    for _ in range(20):
        if int(await redis.get(f"cp:event-streams:{user.id}") or 0) == 0:
            return
        await asyncio.sleep(0.1)
    raise AssertionError("the stream's place was not released")


# --- what the services publish to owners -------------------------------------------------


@asynccontextmanager
async def _owner_channel(redis: Redis, user_id: uuid.UUID) -> AsyncIterator[list[dict[str, Any]]]:
    received: list[dict[str, Any]] = []
    pubsub = redis.pubsub()
    await pubsub.subscribe(events.user_channel(user_id))

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


async def test_a_reservations_life_reaches_its_owner_in_order(
    world: World, redis: Redis, provider: MockPaymentProvider
) -> None:
    slot = await world.slot(quantity=5)
    customer, _ = await user_with_token(world, UserRole.CUSTOMER)
    async with _owner_channel(redis, customer.id) as received:
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
    first_id, second_id = str(first.reservation.id), str(second.reservation.id)
    assert [(n["reservation_id"], n["change"]) for n in received] == [
        (first_id, "created"),
        (first_id, "paid"),
        (first_id, "cancelled"),
        (second_id, "created"),
        (second_id, "expired"),
    ]
    for notice in received:  # every one is a valid customer notice
        assert owner_view(json.dumps(notice)) is not None


async def test_a_pickup_reaches_its_owner(
    world: World, redis: Redis, provider: MockPaymentProvider, make_machine: MakeMachine
) -> None:
    fake, machine, slots = await make_machine({"A1": 3})
    customer, _ = await user_with_token(world, UserRole.CUSTOMER)
    async with world.sessions() as session:
        view, _ = await create_reservation(
            session, redis, customer, ReservationCreate(slot_id=slots["A1"].id)
        )
        result = await checkout(
            session, redis, provider, customer, view.reservation.id, "pm_card_ok"
        )
    assert result.pickup is not None
    await fake.connect()

    async with _owner_channel(redis, customer.id) as received:
        verified = await fake.request("verify_code", credential=result.pickup.code)
        assert verified["ok"] is True
        dispense = await fake.recv()
        await fake.request("dispense_result", command_id=dispense["command_id"],
                           outcome="dispensed", units_dispensed=1)  # fmt: skip
    assert [(n["change"], n["machine_id"]) for n in received] == [
        ("dispensing", str(machine.id)),
        ("dispensed", str(machine.id)),
    ]


# --- the customer filter, notice by notice -----------------------------------------------

MID = "9d6f0a52-3f7a-4f0e-9a43-2a9f5d3f1c11"
AT = "2026-10-29T12:00:00+00:00"


@pytest.mark.parametrize(
    ("topic", "change", "expected"),
    [
        ("stock", "restock", ("stock", "availability")),
        ("stock", "dispensed", ("stock", "availability")),
        ("stock", "corrected", ("stock", "availability")),
        ("reservation", "created", ("stock", "availability")),
        ("reservation", "cancelled", ("stock", "availability")),
        ("reservation", "expired", ("stock", "availability")),
        ("reservation", "paid", None),
        ("reservation", "dispensing", None),
        ("reservation", "dispensed", None),  # arrives as a stock notice too
        ("machine", "connected", ("machine", "online")),
        ("machine", "disconnected", ("machine", "offline")),
        ("machine", "status", ("machine", "status")),
        ("machine", "telemetry", None),
        ("machine", "fault", None),
    ],
)
def test_customer_view_of_operator_notices(
    topic: str, change: str, expected: tuple[str, str] | None
) -> None:
    raw = json.dumps({"topic": topic, "machine_id": MID, "change": change, "at": AT})
    notice = customer_view(raw)
    if expected is None:
        assert notice is None
        return
    assert notice is not None
    assert (notice.topic, notice.change, str(notice.machine_id)) == (*expected, MID)
    assert notice.reservation_id is None


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "not json",
        "[]",
        "{}",
        json.dumps({"topic": "stock", "machine_id": "x", "change": "c", "at": AT}),
    ],  # fmt: skip
)
def test_malformed_notices_are_dropped(raw: str) -> None:
    assert customer_view(raw) is None
    assert owner_view(raw) is None


async def test_publishing_to_an_owner_never_fails_the_operation() -> None:
    class Broken:
        async def publish(self, *_: Any) -> int:
            raise RedisError("down")

    await events.publish_to_owner(
        Broken(),  # type: ignore[arg-type]
        user_id=uuid.uuid4(),
        reservation_id=uuid.uuid4(),
        machine_id=None,
        change="paid",
    )
