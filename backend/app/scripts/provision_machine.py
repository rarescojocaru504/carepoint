"""Register a machine's Ed25519 key: `python -m app.scripts.provision_machine`.

Two modes:

  # Real hardware: the device generated its key pair; register only the public key.
  python -m app.scripts.provision_machine CP-BUC-0001 --public-key <base64url>

  # Simulator / bench: generate a pair here and write the private key to a file.
  python -m app.scripts.provision_machine CP-BUC-0001 --out keys/CP-BUC-0001.pem

Replacing an existing key requires --rotate (the old key stops working at once).
"""

import argparse
import asyncio
import base64
import binascii
import os
import sys
from pathlib import Path

from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)

from app.core.db import AsyncSessionLocal, engine
from app.services.machine_auth import public_key_bytes
from app.services.machine_provisioning import (
    ProvisioningError,
    provision_new_key,
    register_public_key,
)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("serial_number")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--public-key", help="base64url Ed25519 public key generated on the device")
    mode.add_argument("--out", type=Path, help="generate a key pair; write the private key here")
    parser.add_argument("--rotate", action="store_true", help="replace an existing key")
    return parser.parse_args(argv)


def _write_private_key(path: Path, pem: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # O_EXCL: never overwrite an existing key file; 0600: owner-only.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(pem)


async def _run(args: argparse.Namespace) -> int:
    if args.out is not None and args.out.exists():
        print(f"Refusing to overwrite existing file {args.out}", file=sys.stderr)
        return 1
    try:
        async with AsyncSessionLocal() as session, session.begin():
            if args.public_key is not None:
                try:
                    raw = base64.urlsafe_b64decode(
                        args.public_key + "=" * (-len(args.public_key) % 4)
                    )
                except (binascii.Error, ValueError):
                    print("--public-key is not valid base64url", file=sys.stderr)
                    return 1
                await register_public_key(session, args.serial_number, raw, rotate=args.rotate)
                public = raw
            else:
                private_key = await provision_new_key(
                    session, args.serial_number, rotate=args.rotate
                )
                pem = private_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
                _write_private_key(args.out, pem)
                public = public_key_bytes(private_key)
    except ProvisioningError as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        await engine.dispose()

    print(f"Registered key for {args.serial_number}")
    print(f"  public key: {base64.urlsafe_b64encode(public).rstrip(b'=').decode()}")
    if args.out is not None:
        print(f"  private key written to {args.out} (mode 0600).")
        print("  Copy it to the machine, then delete it here.")
    return 0


def main(argv: list[str] | None = None) -> None:
    sys.exit(asyncio.run(_run(_parse_args(argv))))


if __name__ == "__main__":
    main()
