"""In-process background jobs: expiry sweeper and reconciliation (ADR-013).

Each job runs in its own loop with its own sessions; a failing pass is logged
and retried next interval, never allowed to kill the loop. Every job is safe to
run on several app instances at once (SKIP LOCKED sweeps, diff-based and
compare-and-set reconciliation), so no leader election is needed.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from app.core.config import get_settings
from app.core.db import AsyncSessionLocal
from app.core.redis import redis_client
from app.services.hold_reconciliation import reconcile_all
from app.services.reservation_expiry import expire_due_reservations

logger = logging.getLogger(__name__)


async def _sweep_once() -> None:
    async with AsyncSessionLocal() as session:
        await expire_due_reservations(session, redis_client)


async def _reconcile_once() -> None:
    async with AsyncSessionLocal() as session:
        report = await reconcile_all(session, redis_client)
    if report.holds_added or report.holds_removed or report.quantities_fixed:
        logger.warning("Hold reconciliation: %s", report)


async def _every(name: str, seconds: int, job: Callable[[], Awaitable[None]]) -> None:
    while True:
        try:
            await job()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Background job %s failed; retrying in %ss", name, seconds)
        await asyncio.sleep(seconds)


def start_background_workers() -> list[asyncio.Task[None]]:
    settings = get_settings()
    if not settings.background_workers_enabled:
        return []
    return [
        # Reconcile first thing at startup, then periodically.
        asyncio.create_task(
            _every("reconcile", settings.reconcile_interval_seconds, _reconcile_once)
        ),
        asyncio.create_task(
            _every("expiry-sweep", settings.expiry_sweep_interval_seconds, _sweep_once)
        ),
    ]


async def stop_background_workers(tasks: list[asyncio.Task[None]]) -> None:
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
