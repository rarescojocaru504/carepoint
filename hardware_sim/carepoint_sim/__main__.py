"""Run the simulator: `python -m carepoint_sim --config config/demo.json`."""

import argparse
import asyncio
import logging
import signal
from pathlib import Path

from aiohttp import web

from carepoint_sim.config import load_config
from carepoint_sim.kiosk_api import build_app
from carepoint_sim.machine import SimulatedMachine

logger = logging.getLogger("carepoint_sim")


async def _wait_for_keys(keys_dir: Path, serials: list[str]) -> None:
    """The backend issues the demo keys at startup (app.scripts.demo_init)."""
    while missing := [s for s in serials if not (keys_dir / f"{s}.pem").exists()]:
        logger.info("Waiting for machine keys: %s", ", ".join(missing))
        await asyncio.sleep(2)


async def _main(config_path: Path) -> None:
    config = load_config(config_path)
    serials = [m.serial for m in config.machines]
    await _wait_for_keys(config.keys_dir, serials)
    machines = {m.serial: SimulatedMachine(m, config) for m in config.machines}

    runner = web.AppRunner(build_app(machines, config))
    await runner.setup()
    await web.TCPSite(runner, config.http_host, config.http_port).start()
    logger.info("Kiosk API on http://%s:%d, %d machine(s)", config.http_host,
                config.http_port, len(machines))  # fmt: skip

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    tasks = [asyncio.create_task(machine.run()) for machine in machines.values()]
    await stop.wait()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await runner.cleanup()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/demo.json"))
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(
        level=args.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    asyncio.run(_main(args.config))


if __name__ == "__main__":
    main()
