"""Live notices for the customer app (ADR-026): server-sent events.

    GET /api/v1/events     Authorization: Bearer <access token>

Each event's data is a `CustomerNotice`. A customer sees which machines'
availability or status changed (anyone may see that, ADR-011) and notices
about their own reservations only, never other people's. How the stream
authorises, ends and keeps alive is in app/api/sse.py.
"""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import RedisClient
from app.api.sse import authorize_stream, event_stream
from app.core.config import get_settings
from app.core.db import get_session_factory
from app.schemas.events import CustomerNotice
from app.services import events

router = APIRouter(tags=["events"])

_bearer = HTTPBearer(auto_error=False)


def _for_customer(channel: str, data: str) -> str | None:
    notice = events.customer_view(data) if channel == events.CHANNEL else events.owner_view(data)
    return notice.model_dump_json() if notice is not None else None


@router.get(
    "/events",
    response_class=StreamingResponse,
    responses={
        200: {
            "model": CustomerNotice,
            "content": {"text/event-stream": {}},
            "description": "A stream of `data: <CustomerNotice JSON>` events",
        },
        401: {"description": "Not authenticated"},
        429: {"description": "Too many open streams for this user"},
        503: {"description": "Service unavailable"},
    },
)
async def customer_events(
    request: Request,
    redis: RedisClient,
    sessions: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> StreamingResponse:
    """Live notices: machine availability and status, and your own reservations."""
    user, expires_at = await authorize_stream(sessions, credentials)
    settings = get_settings()
    ttl = settings.access_token_ttl_seconds + 120  # outlives any stream (ADR-026)
    try:
        allowed = await events.acquire_stream(
            redis, user.id, limit=settings.customer_event_streams_per_user, ttl=ttl
        )
    except RedisError:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Service unavailable") from None
    if not allowed:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many open event streams")

    async def release() -> None:
        await events.release_stream(redis, user.id)

    pubsub = redis.pubsub()
    try:
        await pubsub.subscribe(events.CHANNEL, events.user_channel(user.id))
    except RedisError:
        await release()
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Service unavailable") from None
    hello = CustomerNotice(topic="hello", change="subscribed", at=datetime.now(UTC))
    return event_stream(
        request,
        pubsub,
        expires_at,
        hello=hello.model_dump_json(),
        transform=_for_customer,
        on_close=release,
    )
