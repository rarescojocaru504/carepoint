"""Create (or remove) data for the load tests in loadtest/.

    python -m app.scripts.loadtest_fixtures create --users 500 --slots 20 --out fixtures.json
    python -m app.scripts.loadtest_fixtures cleanup

Creates customers with pre-issued access tokens (500 real sign-ups would rightly
trip the sign-up rate limit) and machines with one stocked slot each, placed at
0,0 ("Null Island") so they never appear on the demo map. Tokens last
ACCESS_TOKEN_TTL_SECONDS (15 min by default): run the test straight after.
Refuses to run with ENVIRONMENT=production.
"""

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

from sqlalchemy import delete, select

from app.core.config import get_settings
from app.core.db import AsyncSessionLocal, engine
from app.core.redis import redis_client
from app.core.security import create_access_token
from app.models import (
    Machine,
    MachineSlot,
    Product,
    ProductCategory,
    RefreshToken,
    Reservation,
    User,
    UserRole,
)
from app.services import hold_store

EMAIL_DOMAIN = "loadtest.carepoint"
SERIAL_PREFIX = "CP-LOAD-"
SKU = "LOAD-TEST"
# Not a valid argon2 hash, so no password can ever log in as a load-test user;
# they authenticate only with the tokens this script issues.
UNUSABLE_PASSWORD_HASH = "!"  # noqa: S105


async def _create(users: int, slots: int, quantity: int, out: Path) -> None:
    async with AsyncSessionLocal() as session:
        product = await session.scalar(select(Product).where(Product.sku == SKU))
        if product is None:
            product = Product(sku=SKU, name="Load-test item", category=ProductCategory.ESSENTIALS,
                              price_cents=100, currency=get_settings().currency)  # fmt: skip
            session.add(product)
        customers = [
            User(
                email=f"lt-{uuid.uuid4().hex[:12]}@{EMAIL_DOMAIN}",
                password_hash=UNUSABLE_PASSWORD_HASH,
                full_name="Load test",
                role=UserRole.CUSTOMER,
            )
            for _ in range(users)
        ]
        session.add_all(customers)
        await session.flush()
        created = []
        for _ in range(slots):
            machine = Machine(
                serial_number=f"{SERIAL_PREFIX}{uuid.uuid4().hex[:10].upper()}",
                name="Load test", address="Null Island", location="SRID=4326;POINT(0 0)",
            )  # fmt: skip
            session.add(machine)
            await session.flush()
            slot = MachineSlot(machine_id=machine.id, product_id=product.id, slot_code="A1",
                               quantity=quantity, capacity=max(quantity, 10),
                               low_stock_threshold=0)  # fmt: skip
            session.add(slot)
            await session.flush()
            created.append({"slot_id": str(slot.id), "machine_id": str(machine.id)})
        await session.commit()
    out.write_text(
        json.dumps(
            {
                "tokens": [create_access_token(u.id, u.role)[0] for u in customers],
                "slots": created,
                "quantity": quantity,
            }
        )
    )
    print(f"Created {users} customers and {slots} slots x {quantity} unit(s) -> {out}")


async def _cleanup() -> None:
    async with AsyncSessionLocal() as session:
        user_ids = select(User.id).where(User.email.like(f"%@{EMAIL_DOMAIN}"))
        machine_ids = select(Machine.id).where(Machine.serial_number.like(f"{SERIAL_PREFIX}%"))
        slot_ids = list(
            await session.scalars(
                select(MachineSlot.id).where(MachineSlot.machine_id.in_(machine_ids))
            )
        )
        await session.execute(
            delete(Reservation).where(
                Reservation.user_id.in_(user_ids) | Reservation.slot_id.in_(slot_ids)
            )
        )
        await session.execute(delete(RefreshToken).where(RefreshToken.user_id.in_(user_ids)))
        await session.execute(delete(MachineSlot).where(MachineSlot.id.in_(slot_ids)))
        await session.execute(delete(Machine).where(Machine.id.in_(machine_ids)))
        await session.execute(delete(User).where(User.email.like(f"%@{EMAIL_DOMAIN}")))
        await session.execute(delete(Product).where(Product.sku == SKU))
        await session.commit()
    for slot_id in slot_ids:
        await redis_client.delete(*hold_store.slot_keys(slot_id))
    print(f"Removed load-test data ({len(slot_ids)} slots)")


async def _run(args: argparse.Namespace) -> int:
    if get_settings().environment == "production":
        print("Refusing to touch load-test data with ENVIRONMENT=production.", file=sys.stderr)
        return 1
    try:
        if args.command == "create":
            await _create(args.users, args.slots, args.quantity, args.out)
        else:
            await _cleanup()
    finally:
        await redis_client.aclose()
        await engine.dispose()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("--users", type=int, default=500)
    create.add_argument("--slots", type=int, default=20, help="one slot per machine")
    create.add_argument("--quantity", type=int, default=1, help="units per slot")
    create.add_argument("--out", type=Path, default=Path("fixtures.json"))
    sub.add_parser("cleanup")
    sys.exit(asyncio.run(_run(parser.parse_args())))


if __name__ == "__main__":
    main()
