"""Demo machine keys for the simulator: issued once, kept across restarts."""

import stat
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    load_pem_private_key,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Machine
from app.services.demo_keys import ensure_demo_keys, key_path
from app.services.machine_auth import public_key_bytes
from tests.factories import make_machine

pytestmark = pytest.mark.integration


def _public(path: Path) -> bytes:
    key = load_pem_private_key(path.read_bytes(), password=None)
    assert isinstance(key, Ed25519PrivateKey)
    return public_key_bytes(key)


async def test_keys_are_issued_once_and_then_kept(db_session: AsyncSession, tmp_path: Path) -> None:
    machine = await make_machine(db_session)
    serial = machine.serial_number

    assert await ensure_demo_keys(db_session, tmp_path, [serial]) == {serial: True}
    path = key_path(tmp_path, serial)
    await db_session.refresh(machine)
    assert machine.auth_public_key == _public(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    first = path.read_bytes()

    # Restarting the stack keeps the key, so the simulator stays connected.
    assert await ensure_demo_keys(db_session, tmp_path, [serial]) == {serial: False}
    assert path.read_bytes() == first


async def test_lost_or_stale_key_file_is_replaced(db_session: AsyncSession, tmp_path: Path) -> None:
    machine: Machine = await make_machine(db_session)
    serial = machine.serial_number
    await ensure_demo_keys(db_session, tmp_path, [serial])
    path = key_path(tmp_path, serial)

    # A key that is not the registered one (e.g. the database was re-created).
    stale = Ed25519PrivateKey.generate()
    path.write_bytes(stale.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    assert await ensure_demo_keys(db_session, tmp_path, [serial]) == {serial: True}
    assert _public(path) != public_key_bytes(stale)

    path.unlink()
    assert await ensure_demo_keys(db_session, tmp_path, [serial]) == {serial: True}
    await db_session.refresh(machine)
    assert machine.auth_public_key == _public(path)
