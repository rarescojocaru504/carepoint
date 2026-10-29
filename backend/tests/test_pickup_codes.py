"""Pickup codes: issue, verify at the machine, single use, rotation, brute force."""

import hashlib
import re
import uuid
from datetime import timedelta

import httpx
import pytest
from pydantic import SecretStr, ValidationError
from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.security import create_access_token
from app.models import Machine, MachineSlot, Reservation, ReservationStatus, User
from app.services import dispensing, pickup_codes
from app.services.payments import MOCK_OK, MockPaymentProvider
from tests.factories import make_machine, make_product, make_reservation, make_slot, make_user

pytestmark = pytest.mark.integration


def _auth(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user.id, user.role)[0]}"}


async def _paid(
    api: httpx.AsyncClient, session: AsyncSession, *, quantity: int = 5
) -> tuple[User, Machine, MachineSlot, dict[str, str], str]:
    """A customer with a paid reservation; returns its pickup credential."""
    user = await make_user(session)
    product = await make_product(session)
    machine = await make_machine(session)
    slot = await make_slot(session, machine=machine, product=product, quantity=quantity)
    rid = (
        await api.post("/api/v1/reservations", json={"slot_id": str(slot.id)}, headers=_auth(user))
    ).json()["id"]
    checkout = await api.post(
        f"/api/v1/reservations/{rid}/checkout",
        json={"payment_method": MOCK_OK},
        headers=_auth(user),
    )
    assert checkout.status_code == 201, checkout.text
    assert checkout.headers["cache-control"] == "no-store"
    return user, machine, slot, checkout.json()["pickup"], rid


# --- issuing ------------------------------------------------------------------------


async def test_checkout_issues_code_and_qr(
    api: httpx.AsyncClient, db_session: AsyncSession, provider: MockPaymentProvider
) -> None:
    _, machine, _, pickup, rid = await _paid(api, db_session)
    assert re.fullmatch(r"[0-9]{6}", pickup["code"])
    assert pickup["qr_payload"] == f"CP1:{rid}:{pickup['code']}"

    stored = await db_session.scalar(
        select(Reservation.pickup_code_hash).where(Reservation.id == uuid.UUID(rid))
    )
    assert stored == pickup_codes.code_hash(machine.id, pickup["code"])
    # Neither the code nor an unkeyed hash of it is stored (both are brute-forceable).
    assert pickup["code"] not in stored
    assert stored != hashlib.sha256(pickup["code"].encode()).hexdigest()
    assert stored != hashlib.sha256(f"{machine.id}:{pickup['code']}".encode()).hexdigest()


async def test_repeat_checkout_does_not_reveal_code(
    api: httpx.AsyncClient, db_session: AsyncSession, provider: MockPaymentProvider
) -> None:
    user, _, _, _, rid = await _paid(api, db_session)
    again = await api.post(
        f"/api/v1/reservations/{rid}/checkout", json={"payment_method": MOCK_OK},
        headers=_auth(user),
    )  # fmt: skip
    assert again.status_code == 200
    assert again.json()["pickup"] is None


async def test_unpaid_holds_have_no_code(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    slot = await make_slot(
        db_session, machine=await make_machine(db_session), product=await make_product(db_session),
        quantity=2,
    )  # fmt: skip
    rid = (
        await api.post("/api/v1/reservations", json={"slot_id": str(slot.id)}, headers=_auth(user))
    ).json()["id"]
    assert (
        await db_session.scalar(
            select(Reservation.pickup_code_hash).where(Reservation.id == uuid.UUID(rid))
        )
        is None
    )
    rotate = await api.post(f"/api/v1/reservations/{rid}/pickup-code", headers=_auth(user))
    assert rotate.status_code == 409


# --- verifying at the machine -------------------------------------------------------


async def test_verify_by_code_and_by_qr(
    api: httpx.AsyncClient, db_session: AsyncSession, redis: Redis, provider: MockPaymentProvider
) -> None:
    _, machine, slot, pickup, rid = await _paid(api, db_session)
    for credential in (pickup["code"], pickup["qr_payload"], f"  {pickup['code']}\n"):
        found = await pickup_codes.verify(db_session, redis, machine, credential)
        assert str(found.reservation.id) == rid
        assert found.slot_code == slot.slot_code


async def test_code_is_bound_to_its_machine(
    api: httpx.AsyncClient, db_session: AsyncSession, redis: Redis, provider: MockPaymentProvider
) -> None:
    _, _, _, pickup, _ = await _paid(api, db_session)
    elsewhere = await make_machine(db_session)
    for credential in (pickup["code"], pickup["qr_payload"]):
        with pytest.raises(pickup_codes.InvalidPickupCodeError):
            await pickup_codes.verify(db_session, redis, elsewhere, credential)


@pytest.mark.parametrize(
    "mangle",
    [
        lambda p, r: str((int(p["code"]) + 1) % 1_000_000).zfill(6),  # wrong code
        lambda p, r: f"CP1:{uuid.uuid4()}:{p['code']}",  # right code, wrong reservation
        lambda p, r: p["code"][:5],  # too short
        lambda p, r: "١٢٣٤٥٦",  # Unicode (Arabic-Indic) digits
        lambda p, r: p["code"] + "' OR '1'='1",
        lambda p, r: "",
    ],
    ids=["wrong", "qr-other-reservation", "short", "unicode-digits", "injection", "empty"],
)
async def test_bad_credentials_rejected(
    api: httpx.AsyncClient,
    db_session: AsyncSession,
    redis: Redis,
    provider: MockPaymentProvider,
    mangle: object,
) -> None:
    _, machine, _, pickup, rid = await _paid(api, db_session)
    with pytest.raises(pickup_codes.InvalidPickupCodeError):
        await pickup_codes.verify(db_session, redis, machine, mangle(pickup, rid))  # type: ignore[operator]


@pytest.mark.parametrize("ending", ["dispensed", "cancelled", "expired"])
async def test_code_dies_with_its_reservation(
    api: httpx.AsyncClient,
    db_session: AsyncSession,
    redis: Redis,
    provider: MockPaymentProvider,
    ending: str,
) -> None:
    """Single use: once dispensed (or cancelled / expired) the code is worthless."""
    user, machine, _, pickup, rid = await _paid(api, db_session)
    if ending == "dispensed":
        await dispensing.complete_dispense(db_session, redis, provider, uuid.UUID(rid))
    elif ending == "cancelled":
        await api.delete(f"/api/v1/reservations/{rid}", headers=_auth(user))
    else:
        await db_session.execute(
            update(Reservation)
            .where(Reservation.id == uuid.UUID(rid))
            .values(expires_at=Reservation.expires_at - timedelta(hours=1))
        )
    with pytest.raises(pickup_codes.InvalidPickupCodeError):
        await pickup_codes.verify(db_session, redis, machine, pickup["code"])


# --- rotation -----------------------------------------------------------------------


async def test_rotate_replaces_code(
    api: httpx.AsyncClient, db_session: AsyncSession, redis: Redis, provider: MockPaymentProvider
) -> None:
    user, machine, _, old, rid = await _paid(api, db_session)
    response = await api.post(f"/api/v1/reservations/{rid}/pickup-code", headers=_auth(user))
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    new = response.json()
    assert new["qr_payload"] == f"CP1:{rid}:{new['code']}"

    assert (
        str((await pickup_codes.verify(db_session, redis, machine, new["code"])).reservation.id)
        == rid
    )
    if new["code"] != old["code"]:  # 1-in-a-million chance they coincide
        with pytest.raises(pickup_codes.InvalidPickupCodeError):
            await pickup_codes.verify(db_session, redis, machine, old["code"])


async def test_rotate_someone_elses_reservation(
    api: httpx.AsyncClient, db_session: AsyncSession, provider: MockPaymentProvider
) -> None:
    _, _, _, _, rid = await _paid(api, db_session)
    stranger = await make_user(db_session)
    response = await api.post(f"/api/v1/reservations/{rid}/pickup-code", headers=_auth(stranger))
    assert response.status_code == 404


async def test_rotate_rate_limited(
    api: httpx.AsyncClient,
    db_session: AsyncSession,
    provider: MockPaymentProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "rate_limit_pickup_rotate_user", "2/60")
    user, _, _, _, rid = await _paid(api, db_session)
    statuses = [
        (await api.post(f"/api/v1/reservations/{rid}/pickup-code", headers=_auth(user))).status_code
        for _ in range(3)
    ]
    assert statuses == [200, 200, 429]


# --- uniqueness ---------------------------------------------------------------------


async def test_active_codes_are_unique(db_session: AsyncSession) -> None:
    """The partial unique index: two paid reservations cannot share a hash..."""
    user = await make_user(db_session)
    slot = await make_slot(
        db_session, machine=await make_machine(db_session), product=await make_product(db_session),
        quantity=5,
    )  # fmt: skip
    a = await make_reservation(db_session, user=user, slot=slot, status=ReservationStatus.PAID)
    b = await make_reservation(db_session, user=user, slot=slot, status=ReservationStatus.PAID)
    a.pickup_code_hash = "same-hash"
    await db_session.flush()
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            b.pickup_code_hash = "same-hash"
            await db_session.flush()


async def test_finished_reservations_free_their_code(db_session: AsyncSession) -> None:
    """...but a finished reservation's hash does not block reuse."""
    user = await make_user(db_session)
    slot = await make_slot(
        db_session, machine=await make_machine(db_session), product=await make_product(db_session),
        quantity=5,
    )  # fmt: skip
    old = await make_reservation(
        db_session, user=user, slot=slot, status=ReservationStatus.DISPENSED
    )
    old.pickup_code_hash = "reused-hash"
    new = await make_reservation(db_session, user=user, slot=slot, status=ReservationStatus.PAID)
    new.pickup_code_hash = "reused-hash"
    await db_session.flush()


async def test_collision_is_regenerated(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await make_user(db_session)
    slot = await make_slot(
        db_session, machine=await make_machine(db_session), product=await make_product(db_session),
        quantity=5,
    )  # fmt: skip
    first = await make_reservation(db_session, user=user, slot=slot, status=ReservationStatus.PAID)
    second = await make_reservation(db_session, user=user, slot=slot, status=ReservationStatus.PAID)
    codes = iter(["111111", "111111", "222222"])  # the second draw collides
    monkeypatch.setattr(pickup_codes, "_new_code", lambda: next(codes))

    a = await pickup_codes.assign_code(db_session, first)
    b = await pickup_codes.assign_code(db_session, second)

    assert (a.code, b.code) == ("111111", "222222")


# --- brute force ------------------------------------------------------------------


async def test_wrong_guesses_lock_the_kiosk(
    api: httpx.AsyncClient,
    db_session: AsyncSession,
    redis: Redis,
    provider: MockPaymentProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "rate_limit_pickup_failures_machine", "3/60")
    _, machine, _, pickup, _ = await _paid(api, db_session)
    wrong = str((int(pickup["code"]) + 1) % 1_000_000).zfill(6)
    for _ in range(3):
        with pytest.raises(pickup_codes.InvalidPickupCodeError):
            await pickup_codes.verify(db_session, redis, machine, wrong)
    # Locked out: even the right code is refused until the window passes.
    with pytest.raises(pickup_codes.TooManyAttemptsError) as info:
        await pickup_codes.verify(db_session, redis, machine, pickup["code"])
    assert info.value.retry_after_seconds > 0
    # Another machine is unaffected.
    other_api_machine = await make_machine(db_session)
    with pytest.raises(pickup_codes.InvalidPickupCodeError):
        await pickup_codes.verify(db_session, redis, other_api_machine, wrong)


async def test_successful_pickups_do_not_count_as_failures(
    api: httpx.AsyncClient,
    db_session: AsyncSession,
    redis: Redis,
    provider: MockPaymentProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A busy machine with many legitimate pickups is never locked out."""
    monkeypatch.setattr(get_settings(), "rate_limit_pickup_failures_machine", "2/60")
    _, machine, _, pickup, _ = await _paid(api, db_session)
    for _ in range(5):
        await pickup_codes.verify(db_session, redis, machine, pickup["code"])


# --- configuration ---------------------------------------------------------------


def test_production_requires_real_pepper() -> None:
    strong = SecretStr("x" * 40)
    with pytest.raises(ValidationError, match="PICKUP_CODE_PEPPER"):
        Settings(environment="production", jwt_secret=strong)
    Settings(environment="production", jwt_secret=strong, pickup_code_pepper=SecretStr("y" * 40))


def test_hash_depends_on_pepper(monkeypatch: pytest.MonkeyPatch) -> None:
    machine = uuid.uuid4()
    before = pickup_codes.code_hash(machine, "123456")
    monkeypatch.setattr(get_settings(), "pickup_code_pepper", SecretStr("another-pepper-" * 3))
    assert pickup_codes.code_hash(machine, "123456") != before
