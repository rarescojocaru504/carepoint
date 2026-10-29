"""Prepare the local demo stack: `python -m app.scripts.demo_init --keys-dir DIR`.

Seeds the demo data (idempotent) and issues the demo machines' keys into DIR
for the hardware simulator (app.services.demo_keys). Docker Compose runs this
before starting the API when DEMO_INIT=1. Refuses to run with
ENVIRONMENT=production.
"""

import argparse
import asyncio
import sys
from pathlib import Path

from app.core.config import get_settings
from app.core.db import AsyncSessionLocal, engine
from app.scripts.seed import demo_passwords
from app.services import demo_data
from app.services.demo_keys import ensure_demo_keys
from app.services.seeding import seed_demo_data


async def _run(keys_dir: Path) -> int:
    if get_settings().environment == "production":
        print("Refusing to prepare demo data with ENVIRONMENT=production.", file=sys.stderr)
        return 1
    try:
        async with AsyncSessionLocal() as session:
            async with session.begin():
                report = await seed_demo_data(session, demo_passwords())
            issued = await ensure_demo_keys(
                session, keys_dir, [machine.serial_number for machine in demo_data.MACHINES]
            )
    finally:
        await engine.dispose()
    created = sum(report.created.values())
    fresh = [serial for serial, new in issued.items() if new]
    print(f"demo data: {created} row(s) created; machine keys issued: {fresh or 'none (kept)'}")
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keys-dir", type=Path, required=True)
    sys.exit(asyncio.run(_run(parser.parse_args(argv).keys_dir)))


if __name__ == "__main__":
    main()
