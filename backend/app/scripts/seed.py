"""Seed the demo dataset: `python -m app.scripts.seed`.

Safe to run repeatedly (see app.services.seeding). Refuses to run when
ENVIRONMENT=production. Demo passwords come from SEED_<ROLE>_PASSWORD
environment variables, falling back to documented development defaults.
"""

import asyncio
import os
import sys

from app.core.config import get_settings
from app.core.db import AsyncSessionLocal, engine
from app.models import UserRole
from app.services import demo_data
from app.services.seeding import seed_demo_data

DEV_DEFAULT_PASSWORDS: dict[UserRole, str] = {
    UserRole.ADMIN: "carepoint-admin-demo",
    UserRole.OPERATOR: "carepoint-operator-demo",
    UserRole.CUSTOMER: "carepoint-customer-demo",
}


def _passwords() -> dict[UserRole, str]:
    return {
        role: os.environ.get(f"SEED_{role.value.upper()}_PASSWORD") or default
        for role, default in DEV_DEFAULT_PASSWORDS.items()
    }


async def _run() -> int:
    if get_settings().environment == "production":
        print("Refusing to seed demo data with ENVIRONMENT=production.", file=sys.stderr)
        return 1

    passwords = _passwords()
    try:
        async with AsyncSessionLocal() as session, session.begin():
            report = await seed_demo_data(session, passwords)
    finally:
        await engine.dispose()

    for kind, created in report.created.items():
        print(f"{kind:<9} created {created:>3}, already present {report.existing[kind]:>3}")
    print("\nDemo accounts (passwords from SEED_<ROLE>_PASSWORD or the dev defaults):")
    for user in demo_data.USERS:
        default = passwords[user.role] == DEV_DEFAULT_PASSWORDS[user.role]
        hint = f"  password: {passwords[user.role]}" if default else "  password: (from env)"
        print(f"  {user.role.value:<9} {user.email}{hint}")
    return 0


def main() -> None:
    sys.exit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
