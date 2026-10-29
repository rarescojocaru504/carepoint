"""Server-sent-event streams of Redis pub/sub notices (ADR-022, ADR-026).

Shared by the operators' stream (GET /api/v1/admin/events) and the customers'
(GET /api/v1/events). A stream:
- authorises with a short-lived database session that is closed before
  streaming starts, so an open stream holds no database connection;
- ends when the caller's access token expires; the client reconnects with a
  fresh one, so a revoked or demoted user stops receiving notices within one
  token lifetime;
- sends a keep-alive comment every 15 s and asks proxies not to buffer.

Browsers' EventSource can't send an Authorization header, so clients read
the stream with fetch(); tokens never go in URLs, which end up in logs.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime

import anyio
from fastapi import HTTPException, Request, status
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials
from redis.asyncio.client import PubSub
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import get_current_user
from app.core.security import InvalidTokenError, decode_access_token
from app.models import User

KEEPALIVE_SECONDS = 15.0

# (channel, message data) -> the event's data, or None to drop the message.
Transform = Callable[[str, str], str | None]


async def authorize_stream(
    sessions: async_sessionmaker[AsyncSession],
    credentials: HTTPAuthorizationCredentials | None,
) -> tuple[User, datetime]:
    """The active user behind the token, and when the token expires."""
    async with sessions() as session:
        user = await get_current_user(session, credentials)
    if credentials is None:  # get_current_user already rejected this
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    try:
        return user, decode_access_token(credentials.credentials).expires_at
    except InvalidTokenError:  # it validated a moment ago; expired in between
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token") from None


async def _events(
    request: Request,
    pubsub: PubSub,
    expires_at: datetime,
    hello: str,
    transform: Transform,
    on_close: Callable[[], Awaitable[None]] | None,
) -> AsyncIterator[str]:
    try:
        yield f"data: {hello}\n\n"
        quiet = 0.0
        while datetime.now(UTC) < expires_at and not await request.is_disconnected():
            try:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            except RedisError:
                await asyncio.sleep(1)
                message = None
            if message is not None and message["type"] == "message":
                data = transform(message["channel"], message["data"])
                if data is not None:
                    quiet = 0.0
                    yield f"data: {data}\n\n"
                    continue
            quiet += 1.0
            if quiet >= KEEPALIVE_SECONDS:
                quiet = 0.0
                yield ": keep-alive\n\n"
    finally:
        # A client that goes away cancels this generator through an anyio
        # cancel scope, which cancels every await inside it again; unshielded,
        # the cleanup below would never run (and a stream's place would only be
        # freed by its TTL, ADR-026).
        with anyio.CancelScope(shield=True):
            try:
                await pubsub.aclose()  # type: ignore[no-untyped-call]  # untyped in redis-py 5.2
            except RedisError:
                pass
            if on_close is not None:
                await on_close()


def event_stream(
    request: Request,
    pubsub: PubSub,
    expires_at: datetime,
    *,
    hello: str,
    transform: Transform,
    on_close: Callable[[], Awaitable[None]] | None = None,
) -> StreamingResponse:
    """Stream the subscribed channels' messages, passed through `transform`."""
    return StreamingResponse(
        _events(request, pubsub, expires_at, hello, transform, on_close),
        media_type="text/event-stream",
        # no-transform/X-Accel-Buffering: stop proxies from buffering the stream.
        headers={"Cache-Control": "no-store, no-transform", "X-Accel-Buffering": "no"},
    )
