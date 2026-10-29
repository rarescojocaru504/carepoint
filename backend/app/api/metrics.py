"""GET /metrics: Prometheus metrics (ADR-029).

Not public: business numbers (active reservations, machines offline) are
for operators. A scraper sends `Authorization: Bearer <METRICS_TOKEN>`; a
signed-in operator or admin can read them with their access token too.
"""

import hmac

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.api.deps import DbSession, get_current_user
from app.core.config import get_settings
from app.core.metrics import REGISTRY
from app.models import UserRole
from app.services.ops_metrics import refresh_business_gauges

router = APIRouter(tags=["health"])

_bearer = HTTPBearer(auto_error=False)


@router.get("/metrics", include_in_schema=False)
async def metrics(
    session: DbSession,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> Response:
    token = get_settings().metrics_token
    is_scraper = (
        credentials is not None
        and token is not None
        and hmac.compare_digest(credentials.credentials.encode(), token.get_secret_value().encode())
    )
    if not is_scraper:
        user = await get_current_user(session, credentials)  # 401 if not signed in
        if user.role not in (UserRole.OPERATOR, UserRole.ADMIN):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Forbidden")
    await refresh_business_gauges(session)
    return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)
