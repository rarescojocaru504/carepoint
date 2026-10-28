"""Machine request signing (CAREPOINT-ED25519-V1), mostly from an attacker's view."""

import json
import time
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.redis import get_redis
from app.main import app
from app.models import Machine, MachineStatus
from app.services.machine_auth import (
    HEADER_NONCE,
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
    public_key_bytes,
    sign_request,
)
from app.services.machine_provisioning import (
    InvalidPublicKeyError,
    KeyAlreadyRegisteredError,
    MachineNotFoundError,
    provision_new_key,
    register_public_key,
)
from tests.factories import make_machine

pytestmark = pytest.mark.integration

PATH = "/api/v1/machine/heartbeat"
BODY = {"firmware_version": "1.2.3", "uptime_seconds": 3600}


def _body(data: dict[str, Any] | None = None) -> bytes:
    return json.dumps(data if data is not None else BODY, separators=(",", ":")).encode()


async def _provisioned(
    session: AsyncSession, **overrides: Any
) -> tuple[Machine, Ed25519PrivateKey]:
    machine = await make_machine(session, **overrides)
    key = await provision_new_key(session, machine.serial_number)
    return machine, key


async def _send(
    api: httpx.AsyncClient,
    headers: dict[str, str],
    body: bytes | None = None,
    path: str = PATH,
    method: str = "POST",
) -> httpx.Response:
    return await api.request(
        method,
        path,
        content=_body() if body is None else body,
        headers={"Content-Type": "application/json", **headers},
    )


def _signed(machine: Machine, key: Ed25519PrivateKey, **overrides: Any) -> dict[str, str]:
    params: dict[str, Any] = {
        "serial_number": machine.serial_number,
        "method": "POST",
        "path_and_query": PATH,
        "body": _body(),
    } | overrides
    return sign_request(key, **params)


# --- the happy path --------------------------------------------------------------


async def test_signed_heartbeat_accepted(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    machine, key = await _provisioned(db_session)
    response = await _send(api, _signed(machine, key))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["machine_id"] == str(machine.id)
    assert body["status"] == "active"
    await db_session.refresh(machine)
    assert machine.last_seen_at is not None
    assert machine.firmware_version == "1.2.3"


async def test_machine_in_maintenance_can_still_authenticate(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    machine, key = await _provisioned(db_session, status=MachineStatus.MAINTENANCE)
    response = await _send(api, _signed(machine, key))
    assert response.status_code == 200
    assert response.json()["status"] == "maintenance"


# --- tampering & forgery ---------------------------------------------------------


@pytest.mark.parametrize(
    ("description", "mutate"),
    [
        ("body changed after signing",
         lambda m, k: (_signed(m, k), _body({"firmware_version": "6.6.6", "uptime_seconds": 1}))),
        ("signed for another path",
         lambda m, k: (_signed(m, k, path_and_query="/api/v1/machine/other"), None)),
        ("signed for another method",
         lambda m, k: (_signed(m, k, method="PUT"), None)),
        ("query string appended after signing",
         lambda m, k: (_signed(m, k), None, PATH + "?x=1")),
        ("signed by a different key",
         lambda m, k: (_signed(m, Ed25519PrivateKey.generate()), None)),
        ("timestamp too old",
         lambda m, k: (_signed(m, k, timestamp=int(time.time()) - 3600), None)),
        ("timestamp in the future",
         lambda m, k: (_signed(m, k, timestamp=int(time.time()) + 3600), None)),
        ("timestamp header edited after signing",
         lambda m, k: ({**_signed(m, k), HEADER_TIMESTAMP: str(int(time.time()) + 1)}, None)),
        ("nonce header edited after signing",
         lambda m, k: ({**_signed(m, k), HEADER_NONCE: "B" * 22}, None)),
        ("malformed signature",
         lambda m, k: ({**_signed(m, k), HEADER_SIGNATURE: "not-a-signature"}, None)),
        ("nonce too short",
         lambda m, k: (_signed(m, k, nonce="short"), None)),
        ("missing signature header",
         lambda m, k: ({h: v for h, v in _signed(m, k).items() if h != HEADER_SIGNATURE}, None)),
        ("no headers at all", lambda m, k: ({}, None)),
    ],
)  # fmt: skip
async def test_tampered_requests_rejected(
    api: httpx.AsyncClient, db_session: AsyncSession, description: str, mutate: Any
) -> None:
    machine, key = await _provisioned(db_session)
    headers, body, *path = mutate(machine, key)
    response = await _send(api, headers, body, path=path[0] if path else PATH)
    assert response.status_code == 401, description
    assert response.json() == {"detail": "Invalid machine signature"}  # no oracle
    await db_session.refresh(machine)
    assert machine.last_seen_at is None


async def test_replayed_request_rejected(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    machine, key = await _provisioned(db_session)
    headers = _signed(machine, key)
    assert (await _send(api, headers)).status_code == 200
    assert (await _send(api, headers)).status_code == 401


async def test_unsigned_junk_does_not_consume_nonces(
    api: httpx.AsyncClient, db_session: AsyncSession, redis: Redis
) -> None:
    """An attacker who learns a nonce before the real request cannot burn it."""
    machine, key = await _provisioned(db_session)
    real = _signed(machine, key)
    forged = {
        **real,
        HEADER_SIGNATURE: _signed(machine, Ed25519PrivateKey.generate())[HEADER_SIGNATURE],
    }
    assert (await _send(api, forged)).status_code == 401
    assert (await _send(api, real)).status_code == 200


async def test_impersonating_another_machine_fails(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    victim, _ = await _provisioned(db_session)
    attacker, attacker_key = await _provisioned(db_session)
    headers = _signed(victim, attacker_key)  # claims victim's serial, signs with own key
    assert (await _send(api, headers)).status_code == 401
    await db_session.refresh(victim)
    assert victim.last_seen_at is None
    assert attacker.last_seen_at is None


@pytest.mark.parametrize(
    "setup",
    ["unknown-serial", "no-key", "decommissioned"],
)
async def test_machine_state_rejections(
    api: httpx.AsyncClient, db_session: AsyncSession, setup: str
) -> None:
    key = Ed25519PrivateKey.generate()
    if setup == "unknown-serial":
        machine = Machine(serial_number="CP-NOPE-9999")
    elif setup == "no-key":
        machine = await make_machine(db_session)
    else:
        machine, key = await _provisioned(db_session, status=MachineStatus.DECOMMISSIONED)
    response = await _send(api, _signed(machine, key))
    assert response.status_code == 401


async def test_auth_runs_before_body_validation(api: httpx.AsyncClient) -> None:
    """Unauthenticated callers learn nothing about the payload schema."""
    response = await _send(api, {}, body=b'{"garbage": true}')
    assert response.status_code == 401


async def test_signed_but_invalid_body_rejected(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    machine, key = await _provisioned(db_session)
    for bad in (
        {"firmware_version": "1.0; DROP TABLE", "uptime_seconds": 1},
        {"firmware_version": "1.0", "uptime_seconds": -5},
        {"firmware_version": "1.0", "uptime_seconds": "5"},
        {"firmware_version": "1.0", "uptime_seconds": 5, "extra": 1},
    ):
        body = _body(bad)
        response = await _send(api, _signed(machine, key, body=body), body=body)
        assert response.status_code == 422, bad


async def test_replay_store_down_fails_closed(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    machine, key = await _provisioned(db_session)
    dead: Redis = Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.2)

    async def _dead() -> Redis:
        return dead

    app.dependency_overrides[get_redis] = _dead
    try:
        assert (await _send(api, _signed(machine, key))).status_code == 503
    finally:
        await dead.aclose()


async def test_nonce_expires_after_window(
    api: httpx.AsyncClient, db_session: AsyncSession, redis: Redis
) -> None:
    machine, key = await _provisioned(db_session)
    await _send(api, _signed(machine, key))
    keys = await redis.keys("machine-nonce:*")
    assert len(keys) == 1
    ttl = await redis.ttl(keys[0])
    assert 0 < ttl <= 2 * get_settings().machine_auth_max_skew_seconds


# --- provisioning ----------------------------------------------------------------


async def test_register_device_generated_public_key(db_session: AsyncSession) -> None:
    machine = await make_machine(db_session)
    device_key = Ed25519PrivateKey.generate()  # lives on the device
    await register_public_key(db_session, machine.serial_number, public_key_bytes(device_key))
    assert machine.auth_public_key == public_key_bytes(device_key)


async def test_provisioning_refuses_silent_key_replacement(db_session: AsyncSession) -> None:
    machine, first = await _provisioned(db_session)
    with pytest.raises(KeyAlreadyRegisteredError):
        await provision_new_key(db_session, machine.serial_number)
    second = await provision_new_key(db_session, machine.serial_number, rotate=True)
    assert machine.auth_public_key == public_key_bytes(second) != public_key_bytes(first)


async def test_provisioning_validation(db_session: AsyncSession) -> None:
    machine = await make_machine(db_session)
    with pytest.raises(InvalidPublicKeyError):
        await register_public_key(db_session, machine.serial_number, b"short")
    with pytest.raises(MachineNotFoundError):
        await register_public_key(
            db_session, "CP-NOPE-0000", public_key_bytes(Ed25519PrivateKey.generate())
        )


async def test_rotated_key_invalidates_old_one(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    machine, old_key = await _provisioned(db_session)
    new_key = await provision_new_key(db_session, machine.serial_number, rotate=True)
    assert (await _send(api, _signed(machine, old_key))).status_code == 401
    assert (await _send(api, _signed(machine, new_key))).status_code == 200


# --- spec conformance ------------------------------------------------------------

# Known-answer vector for device firmware authors (also in ADR-010). Ed25519 is
# deterministic, so this exact signature is the only valid one. Independently
# cross-checked with `openssl pkeyutl -sign -rawin`.
KAT_PRIVATE_SEED = bytes(range(32))
KAT_PUBLIC_KEY_B64URL = "A6EHv_POEL4dcN0Y50vAmWfk1jCbpQ1fHdyGZBJVMbg"
KAT_BODY = b'{"firmware_version":"1.0.0","uptime_seconds":42}'
KAT_SIGNATURE = (
    "3rLGXdo8ZelsHHTse4YjpmEwH21fWUHkSdCbEjHI33PrIhvHbpnU1OuCuQdnHHY25lFAcRF8wsJS2ZObgMTsCw"
)


def test_known_answer_vector() -> None:
    import base64

    key = Ed25519PrivateKey.from_private_bytes(KAT_PRIVATE_SEED)
    public = base64.urlsafe_b64encode(public_key_bytes(key)).rstrip(b"=").decode()
    assert public == KAT_PUBLIC_KEY_B64URL
    headers = sign_request(
        key,
        serial_number="CP-TEST-0001",
        method="POST",
        path_and_query="/api/v1/machine/heartbeat",
        body=KAT_BODY,
        timestamp=1767225600,
        nonce="AAAAAAAAAAAAAAAAAAAAAA",
    )
    assert headers[HEADER_SIGNATURE] == KAT_SIGNATURE
