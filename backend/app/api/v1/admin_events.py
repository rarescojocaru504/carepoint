"""Live change notices for operator dashboards (ADR-022): server-sent events.

    GET /api/v1/admin/events     Authorization: Bearer <access token>

Each event is `data: {"topic", "machine_id", "change", "at"}`; a dashboard
re-reads what changed through the admin API. The stream ends when the access
token expires, and the client reconnects with a fresh one, so a revoked or
demoted operator stops receiving notices within one token lifetime. Browsers'
EventSource cannot send an Authorization header, so dashboards read the stream
with fetch().
"""

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis.asyncio.client import PubSub
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import RedisClient, get_current_user
from app.core.db import get_session_factory
from app.core.security import InvalidTokenError, decode_access_token
from app.models import UserRole
from app.services.events import CHANNEL

router = APIRouter(prefix="/admin", tags=["admin"])

_bearer = HTTPBearer(auto_error=False)
KEEPALIVE_SECONDS = 15.0


async def _authorize(
    sessions: async_sessionmaker[AsyncSession],
    credentials: HTTPAuthorizationCredentials | None,
) -> datetime:
    """Check the caller is an active operator or admin; return when the token
    expires. A short session, released before streaming starts: the stream may
    stay open for many minutes and must not hold a database connection."""
    async with sessions() as session:
        user = await get_current_user(session, credentials)
    if user.role not in (UserRole.OPERATOR, UserRole.ADMIN):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Forbidden")
    if credentials is None:  # get_current_user already rejected this
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    try:
        return decode_access_token(credentials.credentials).expires_at
    except InvalidTokenError:  # it validated a moment ago; expired in between
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token") from None


async def _stream(request: Request, pubsub: PubSub, expires_at: datetime) -> AsyncIterator[str]:
    try:
        hello = {"topic": "hello", "machine_id": None, "change": "subscribed",
                 "at": datetime.now(UTC).isoformat()}  # fmt: skip
        yield f"data: {json.dumps(hello)}\n\n"
        quiet = 0.0
        while datetime.now(UTC) < expires_at and not await request.is_disconnected():
            try:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            except RedisError:
                await asyncio.sleep(1)
                message = None
            if message is not None and message["type"] == "message":
                quiet = 0.0
                yield f"data: {message['data']}\n\n"
                continue
            quiet += 1.0
            if quiet >= KEEPALIVE_SECONDS:
                quiet = 0.0
                yield ": keep-alive\n\n"
    finally:
        try:
            await pubsub.aclose()  # type: ignore[no-untyped-call]  # untyped in redis-py 5.2
        except RedisError:
            pass


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
    expires_at = await _authorize(sessions, credentials)
    pubsub = redis.pubsub()
    try:
        await pubsub.subscribe(CHANNEL)
    except RedisError:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Service unavailable") from None
    return StreamingResponse(
        _stream(request, pubsub, expires_at),
        media_type="text/event-stream",
        # no-transform/X-Accel-Buffering: stop proxies from buffering the stream.
        headers={"Cache-Control": "no-store, no-transform", "X-Accel-Buffering": "no"},
    )
