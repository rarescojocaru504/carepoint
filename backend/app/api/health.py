from fastapi import APIRouter, Depends, Response, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.redis import get_redis
from app.schemas.health import HealthResponse, ProbeStatus, ReadinessResponse
from app.services.health import check_readiness

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Liveness probe: the API process is up. Touches no dependencies."""
    return HealthResponse(status=ProbeStatus.OK)


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadinessResponse}},
)
async def ready(
    response: Response,
    session: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> ReadinessResponse:
    """Readiness probe: PostgreSQL and Redis are both reachable."""
    report = await check_readiness(session, redis)
    if report.status is not ProbeStatus.OK:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return report
