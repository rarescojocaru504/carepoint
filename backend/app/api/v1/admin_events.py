"""Live change notices for operator dashboards (ADR-022): server-sent events.

    GET /api/v1/admin/events     Authorization: Bearer <access token>

Each event is `data: {"topic", "machine_id", "change", "at"}`; a dashboard
re-reads what changed through the admin API. How the stream authorises, ends
and keeps alive is in app/api/sse.py.
"""

import json
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import RedisClient
from app.api.sse import authorize_stream, event_stream
from app.core.db import get_session_factory
from app.models import UserRole
from app.services.events import CHANNEL

router = APIRouter(prefix="/admin", tags=["admin"])

_bearer = HTTPBearer(auto_error=False)


def _as_is(_channel: str, data: str) -> str:
    return data


@router.get(
    "/events",
    response_class=StreamingResponse,
    responses={
        200: {"content": {"text/event-stream": {}}, "description": "Change notices"},
        401: {"description": "Not authenticated"},
        403: {"description": "Forbidden"},
    },
)
async def admin_events(
    request: Request,
    redis: RedisClient,
    sessions: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> StreamingResponse:
    user, expires_at = await authorize_stream(sessions, credentials)
    if user.role not in (UserRole.OPERATOR, UserRole.ADMIN):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Forbidden")
    pubsub = redis.pubsub()
    try:
        await pubsub.subscribe(CHANNEL)
    except RedisError:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Service unavailable") from None
    hello = {"topic": "hello", "machine_id": None, "change": "subscribed",
             "at": datetime.now(UTC).isoformat()}  # fmt: skip
    return event_stream(request, pubsub, expires_at, hello=json.dumps(hello), transform=_as_is)
