"""Machine keys for the local demo stack (ADR-019).

The simulator plays the demo machines, so it needs their private keys. Real
machines generate their own key pair and register only the public half
(ADR-010); for the demo the backend issues keys into a directory shared with
the simulator container. Development only: callers refuse in production.
"""

import os
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    load_pem_private_key,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Machine
from app.services.machine_auth import public_key_bytes
from app.services.machine_provisioning import provision_new_key


def key_path(keys_dir: Path, serial_number: str) -> Path:
    return keys_dir / f"{serial_number}.pem"


def _load(path: Path) -> Ed25519PrivateKey | None:
    try:
        key = load_pem_private_key(path.read_bytes(), password=None)
    except (OSError, ValueError):
        return None
    return key if isinstance(key, Ed25519PrivateKey) else None


def _write(path: Path, key: Ed25519PrivateKey) -> None:
    pem = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(pem)
    tmp.replace(path)  # atomic: the simulator never reads a half-written key


async def ensure_demo_keys(
    session: AsyncSession, keys_dir: Path, serial_numbers: list[str]
) -> dict[str, bool]:
    """Make sure each machine has a key whose private half is in `keys_dir`.

    Idempotent: a key file that matches the registered public key is kept, so
    restarting the stack does not disconnect the simulator. A missing or
    mismatched file (fresh volume, re-created database) gets a new key pair,
    replacing the registered one. Returns serial -> True if a key was issued.
    Commits.
    """
    keys_dir.mkdir(parents=True, exist_ok=True)
    issued: dict[str, bool] = {}
    for serial in serial_numbers:
        registered = await session.scalar(
            select(Machine.auth_public_key).where(Machine.serial_number == serial)
        )
        path = key_path(keys_dir, serial)
        existing = _load(path)
        if existing is not None and registered == public_key_bytes(existing):
            issued[serial] = False
            continue
        _write(path, await provision_new_key(session, serial, rotate=True))
        issued[serial] = True
    await session.commit()
    return issued
