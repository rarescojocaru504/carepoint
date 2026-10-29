"""CAREPOINT-ED25519-V1 request signing (ADR-010), as firmware implements it.

Written from the spec, not imported from the backend: the simulator is the
proof that a machine can be built from docs/machine-protocol.md alone.
"""

import base64
import hashlib
import secrets
import time
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key

SCHEME = "CAREPOINT-ED25519-V1"
GATEWAY_PATH = "/api/v1/ws/machines"


def canonical(
    *, method: str, path: str, serial: str, timestamp: int, nonce: str, body: bytes
) -> bytes:
    return "\n".join(
        (SCHEME, method, path, serial, str(timestamp), nonce, hashlib.sha256(body).hexdigest())
    ).encode()


def signed_headers(
    key: Ed25519PrivateKey,
    serial: str,
    *,
    method: str = "GET",
    path: str = GATEWAY_PATH,
    body: bytes = b"",
    timestamp: int | None = None,
    nonce: str | None = None,
) -> dict[str, str]:
    ts = int(time.time()) if timestamp is None else timestamp
    n = secrets.token_urlsafe(16) if nonce is None else nonce
    message = canonical(method=method, path=path, serial=serial, timestamp=ts, nonce=n, body=body)
    signature = base64.urlsafe_b64encode(key.sign(message)).rstrip(b"=").decode()
    return {
        "X-CarePoint-Machine": serial,
        "X-CarePoint-Timestamp": str(ts),
        "X-CarePoint-Nonce": n,
        "X-CarePoint-Signature": signature,
    }


def load_key(path: Path) -> Ed25519PrivateKey:
    key = load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError(f"{path} is not an Ed25519 private key")
    return key
