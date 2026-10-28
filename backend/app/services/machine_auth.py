"""Ed25519 request signing for machines (scheme CAREPOINT-ED25519-V1, ADR-010).

Request headers:
    X-CarePoint-Machine    machine serial number
    X-CarePoint-Timestamp  Unix time in whole seconds
    X-CarePoint-Nonce      22-64 base64url chars (>= 128 bits), single use
    X-CarePoint-Signature  base64url (unpadded) Ed25519 signature, 64 bytes

The signature covers this UTF-8 string, lines joined with "\\n", no trailing newline:

    CAREPOINT-ED25519-V1
    <HTTP method, uppercase>
    <path, plus "?" and the raw query string if there is one>
    <serial number>
    <timestamp>
    <nonce>
    <lowercase hex SHA-256 of the raw request body (of b"" if empty)>

The same scheme signs the WebSocket handshake (method GET, empty body).
"""

import base64
import binascii
import hashlib
import logging
import re
import secrets
import time
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Machine, MachineStatus

logger = logging.getLogger(__name__)

SCHEME = "CAREPOINT-ED25519-V1"
HEADER_MACHINE = "X-CarePoint-Machine"
HEADER_TIMESTAMP = "X-CarePoint-Timestamp"
HEADER_NONCE = "X-CarePoint-Nonce"
HEADER_SIGNATURE = "X-CarePoint-Signature"

_SERIAL_RE = re.compile(r"^[A-Z0-9-]{4,32}$")
_TIMESTAMP_RE = re.compile(r"^[0-9]{1,12}$")
_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{22,64}$")
_SIGNATURE_RE = re.compile(r"^[A-Za-z0-9_-]{86}$")  # 64 bytes, unpadded base64url


class MachineAuthError(Exception):
    """Authentication failed. `reason` is for logs only, never for the client."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class SignedHeaders:
    serial_number: str
    timestamp: int
    nonce: str
    signature: bytes


def body_digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def canonical_request(
    *, method: str, path_and_query: str, serial_number: str, timestamp: int, nonce: str, body: bytes
) -> bytes:
    return "\n".join(
        (
            SCHEME,
            method.upper(),
            path_and_query,
            serial_number,
            str(timestamp),
            nonce,
            body_digest(body),
        )
    ).encode()


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def parse_signed_headers(headers: dict[str, str | None]) -> SignedHeaders:
    """Validate header presence and format (no crypto yet)."""
    serial = headers.get(HEADER_MACHINE)
    timestamp = headers.get(HEADER_TIMESTAMP)
    nonce = headers.get(HEADER_NONCE)
    signature = headers.get(HEADER_SIGNATURE)
    if not (serial and timestamp and nonce and signature):
        raise MachineAuthError("missing signature headers")
    if not _SERIAL_RE.fullmatch(serial):
        raise MachineAuthError("malformed serial number")
    if not _TIMESTAMP_RE.fullmatch(timestamp):
        raise MachineAuthError("malformed timestamp")
    if not _NONCE_RE.fullmatch(nonce):
        raise MachineAuthError("malformed nonce")
    if not _SIGNATURE_RE.fullmatch(signature):
        raise MachineAuthError("malformed signature")
    try:
        raw_signature = _b64url_decode(signature)
    except (binascii.Error, ValueError):
        raise MachineAuthError("undecodable signature") from None
    return SignedHeaders(serial, int(timestamp), nonce, raw_signature)


async def verify_machine_request(
    session: AsyncSession,
    redis: Redis,
    *,
    headers: SignedHeaders,
    method: str,
    path_and_query: str,
    body: bytes,
    max_skew_seconds: int,
    now: float | None = None,
) -> Machine:
    """Authenticate a signed request and return the machine, or raise MachineAuthError.

    Order matters: cheap checks first, the nonce is consumed only after the
    signature verifies (so unsigned junk cannot fill the replay store or burn a
    legitimate machine's nonces).
    """
    current = time.time() if now is None else now
    if abs(current - headers.timestamp) > max_skew_seconds:
        raise MachineAuthError("timestamp outside allowed skew")

    machine = await session.scalar(
        select(Machine).where(Machine.serial_number == headers.serial_number)
    )
    if machine is None:
        raise MachineAuthError("unknown machine")
    if machine.status is MachineStatus.DECOMMISSIONED:
        raise MachineAuthError("machine decommissioned")
    if machine.auth_public_key is None:
        raise MachineAuthError("machine has no registered key")

    message = canonical_request(
        method=method,
        path_and_query=path_and_query,
        serial_number=headers.serial_number,
        timestamp=headers.timestamp,
        nonce=headers.nonce,
        body=body,
    )
    try:
        Ed25519PublicKey.from_public_bytes(machine.auth_public_key).verify(
            headers.signature, message
        )
    except (InvalidSignature, ValueError):
        raise MachineAuthError("bad signature") from None

    # Remember the nonce for the whole window in which its timestamp is valid.
    nonce_key = f"machine-nonce:{machine.id}:{headers.nonce}"
    if not await redis.set(nonce_key, "1", nx=True, ex=2 * max_skew_seconds):
        raise MachineAuthError("replayed nonce")
    return machine


# --- client side (tests, simulator, reference for device firmware) ---------------


def generate_nonce() -> str:
    return secrets.token_urlsafe(16)  # 128 bits -> 22 chars


def sign_request(
    private_key: Ed25519PrivateKey,
    *,
    serial_number: str,
    method: str,
    path_and_query: str,
    body: bytes = b"",
    timestamp: int | None = None,
    nonce: str | None = None,
) -> dict[str, str]:
    """Return the four headers that authenticate this request."""
    ts = int(time.time()) if timestamp is None else timestamp
    n = generate_nonce() if nonce is None else nonce
    message = canonical_request(
        method=method,
        path_and_query=path_and_query,
        serial_number=serial_number,
        timestamp=ts,
        nonce=n,
        body=body,
    )
    return {
        HEADER_MACHINE: serial_number,
        HEADER_TIMESTAMP: str(ts),
        HEADER_NONCE: n,
        HEADER_SIGNATURE: _b64url_encode(private_key.sign(message)),
    }


def public_key_bytes(private_key: Ed25519PrivateKey) -> bytes:
    return private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
