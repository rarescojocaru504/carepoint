"""Pickup codes: what the customer enters (or scans) at the machine (ADR-016).

- A code is 6 random digits; the QR code encodes `CP1:<reservation id>:<code>`.
- Only `HMAC-SHA256(pepper, "<machine id>:<code>")` is stored. With only 10^6
  possible codes a plain hash would be reversed instantly from a database dump;
  the server-side pepper prevents that. Including the machine id binds the code
  to the one machine the item is in.
- A code works only while its reservation is `paid` and unexpired: dispensing,
  cancelling or expiry kills it, so it is single-use by construction.
- No two paid reservations may share a hash (partial unique index), so codes
  are unique per machine while in use; a collision is regenerated.
- Failed attempts are limited per machine against brute force at the kiosk.
"""

import hashlib
import hmac
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import Machine, MachineSlot, Reservation, ReservationStatus, User
from app.services.rate_limit import RateLimitRule, hit, peek

CODE_DIGITS = 6
QR_PREFIX = "CP1"
_CODE_RE = re.compile(r"^[0-9]{6}$")  # ASCII digits only (no Unicode digits)
_QR_RE = re.compile(
    r"^CP1:([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}):([0-9]{6})$"
)
_MAX_COLLISION_RETRIES = 5


class PickupError(Exception):
    """Base class; messages are safe to show on the kiosk."""


class InvalidPickupCodeError(PickupError):
    pass


class TooManyAttemptsError(PickupError):
    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("too many wrong codes, please wait")
        self.retry_after_seconds = retry_after_seconds


class PickupReservationNotFoundError(PickupError):
    pass


class NotPickupableError(PickupError):
    """Only paid, unexpired reservations have a usable code."""


@dataclass(frozen=True)
class PickupCredential:
    code: str
    qr_payload: str
    expires_at: datetime


@dataclass(frozen=True)
class VerifiedPickup:
    reservation: Reservation
    slot_code: str


def code_hash(machine_id: uuid.UUID, code: str) -> str:
    pepper = get_settings().pickup_code_pepper.get_secret_value().encode()
    return hmac.new(pepper, f"{machine_id}:{code}".encode(), hashlib.sha256).hexdigest()


def _new_code() -> str:
    return f"{secrets.randbelow(10**CODE_DIGITS):0{CODE_DIGITS}d}"


async def _machine_of(session: AsyncSession, reservation: Reservation) -> uuid.UUID:
    machine_id: uuid.UUID = (
        await session.execute(
            select(MachineSlot.machine_id).where(MachineSlot.id == reservation.slot_id)
        )
    ).scalar_one()
    return machine_id


async def assign_code(session: AsyncSession, reservation: Reservation) -> PickupCredential:
    """Give a paid reservation a fresh code, replacing any previous one.

    Flushes inside a savepoint so a hash collision with another active code at
    the same machine (unique index) is retried with a new code. The caller
    commits.
    """
    machine_id = await _machine_of(session, reservation)
    for _ in range(_MAX_COLLISION_RETRIES):
        code = _new_code()
        try:
            async with session.begin_nested():
                reservation.pickup_code_hash = code_hash(machine_id, code)
                await session.flush()
        except IntegrityError:
            continue
        return PickupCredential(
            code, f"{QR_PREFIX}:{reservation.id}:{code}", reservation.expires_at
        )
    raise RuntimeError("could not generate a unique pickup code")  # ~impossible: 10^6 space


async def rotate_code(
    session: AsyncSession, user: User, reservation_id: uuid.UUID
) -> PickupCredential:
    """Issue a new code for a paid reservation (lost or leaked code). The old one
    stops working immediately."""
    reservation = await session.scalar(
        select(Reservation)
        .where(Reservation.id == reservation_id, Reservation.user_id == user.id)
        .with_for_update()
    )
    if reservation is None:
        raise PickupReservationNotFoundError("reservation not found")
    if not is_pickupable(reservation):
        raise NotPickupableError("only paid, unexpired reservations have a pickup code")
    credential = await assign_code(session, reservation)
    await session.commit()
    return credential


def is_pickupable(reservation: Reservation) -> bool:
    # Same rule (and clock) as reservations.effective_status.
    return reservation.status is ReservationStatus.PAID and reservation.expires_at > datetime.now(
        UTC
    )


FAILURE_SCOPE = "pickup-failures"


async def verify(
    session: AsyncSession, redis: Redis, machine: Machine, credential: str
) -> VerifiedPickup:
    """Resolve a typed code or scanned QR payload at `machine` to the reservation
    to dispense. Does not change any state (dispensing does).

    Wrong codes count towards a per-machine limit; once it is exceeded every
    attempt, right or wrong, is refused until the window passes, so an attacker
    at the kiosk cannot keep guessing.
    """
    rule = RateLimitRule.parse(get_settings().rate_limit_pickup_failures_machine)
    failures, reset_in = await peek(redis, FAILURE_SCOPE, str(machine.id))
    if failures >= rule.limit:
        raise TooManyAttemptsError(max(reset_in, 1))

    credential = credential.strip()
    reservation_id: uuid.UUID | None = None
    if (qr := _QR_RE.fullmatch(credential)) is not None:
        reservation_id, code = uuid.UUID(qr.group(1)), qr.group(2)
    elif _CODE_RE.fullmatch(credential):
        code = credential
    else:
        await _count_failure(redis, machine.id, rule)
        raise InvalidPickupCodeError("invalid code")

    query = (
        select(Reservation, MachineSlot.slot_code)
        .join(MachineSlot, MachineSlot.id == Reservation.slot_id)
        .where(
            Reservation.pickup_code_hash == code_hash(machine.id, code),
            Reservation.status == ReservationStatus.PAID,
            Reservation.expires_at > func.now(),
            MachineSlot.machine_id == machine.id,
        )
    )
    if reservation_id is not None:
        query = query.where(Reservation.id == reservation_id)
    row = (await session.execute(query)).one_or_none()
    if row is None:
        await _count_failure(redis, machine.id, rule)
        raise InvalidPickupCodeError("invalid code")
    reservation, slot_code = row
    return VerifiedPickup(reservation, slot_code)


async def _count_failure(redis: Redis, machine_id: uuid.UUID, rule: RateLimitRule) -> None:
    # The same counter `peek` reads: atomic INCR + EXPIRE (rate_limit.hit).
    await hit(redis, FAILURE_SCOPE, str(machine_id), rule)
