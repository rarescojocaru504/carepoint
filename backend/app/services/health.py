import asyncio
import logging

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.health import ProbeStatus, ReadinessChecks, ReadinessResponse

logger = logging.getLogger(__name__)

CHECK_TIMEOUT_SECONDS = 2.0


async def check_database(session: AsyncSession) -> ProbeStatus:
    try:
        async with asyncio.timeout(CHECK_TIMEOUT_SECONDS):
            await session.execute(text("SELECT 1"))
    except Exception:
        logger.exception("Readiness: database check failed")
        return ProbeStatus.UNAVAILABLE
    return ProbeStatus.OK


async def check_redis(redis: Redis) -> ProbeStatus:
    try:
        async with asyncio.timeout(CHECK_TIMEOUT_SECONDS):
            await redis.ping()
    except Exception:
        logger.exception("Readiness: redis check failed")
        return ProbeStatus.UNAVAILABLE
    return ProbeStatus.OK


async def check_readiness(session: AsyncSession, redis: Redis) -> ReadinessResponse:
    """Probe every hard dependency concurrently.

    Failure details are logged, never returned, so the public endpoint does not
    leak hostnames or driver errors.
    """
    database, redis_status = await asyncio.gather(check_database(session), check_redis(redis))
    checks = ReadinessChecks(database=database, redis=redis_status)
    healthy = all(status is ProbeStatus.OK for status in (database, redis_status))
    return ReadinessResponse(
        status=ProbeStatus.OK if healthy else ProbeStatus.UNAVAILABLE,
        checks=checks,
    )
