"""Registering machine public keys (ADR-010)."""

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Machine
from app.services.machine_auth import public_key_bytes


class ProvisioningError(Exception):
    pass


class MachineNotFoundError(ProvisioningError):
    pass


class KeyAlreadyRegisteredError(ProvisioningError):
    """Refusing to silently replace a key; pass rotate=True to do it on purpose."""


class InvalidPublicKeyError(ProvisioningError):
    pass


async def register_public_key(
    session: AsyncSession, serial_number: str, public_key: bytes, *, rotate: bool = False
) -> Machine:
    """Store a machine's Ed25519 public key. Flushes, does not commit.

    Preferred for real hardware: the device generates its own key pair and only
    the public half ever leaves it.
    """
    if len(public_key) != 32:
        raise InvalidPublicKeyError("an Ed25519 public key is exactly 32 bytes")
    try:
        Ed25519PublicKey.from_public_bytes(public_key)
    except ValueError as exc:
        raise InvalidPublicKeyError(str(exc)) from exc

    machine = await session.scalar(select(Machine).where(Machine.serial_number == serial_number))
    if machine is None:
        raise MachineNotFoundError(serial_number)
    if machine.auth_public_key is not None and not rotate:
        raise KeyAlreadyRegisteredError(serial_number)
    machine.auth_public_key = public_key
    await session.flush()
    return machine


async def provision_new_key(
    session: AsyncSession, serial_number: str, *, rotate: bool = False
) -> Ed25519PrivateKey:
    """Generate a key pair server-side (simulators, bench setups) and register it.

    The caller must hand the private key to the machine and then discard it;
    the server never stores it.
    """
    private_key = Ed25519PrivateKey.generate()
    await register_public_key(session, serial_number, public_key_bytes(private_key), rotate=rotate)
    return private_key
