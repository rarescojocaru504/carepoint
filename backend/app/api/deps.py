"""Shared FastAPI dependencies: authentication, authorisation, rate limiting."""

import logging
from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.db import get_db
from app.core.redis import get_redis
from app.core.security import InvalidTokenError, decode_access_token
from app.models import User, UserRole
from app.services.rate_limit import RateLimitRule, hit

logger = logging.getLogger(__name__)

DbSession = Annotated[AsyncSession, Depends(get_db)]
RedisClient = Annotated[Redis, Depends(get_redis)]

_bearer = HTTPBearer(auto_error=False, description="Access token from POST /auth/login")


def _unauthorized(detail: str = "Not authenticated") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_user(
    session: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> User:
    """Resolve the bearer token to an active user.

    The role and active flag come from the database, not the token, so a
    demotion or deactivation takes effect immediately instead of when the
    access token happens to expire.
    """
    if credentials is None:
        raise _unauthorized()
    try:
        claims = decode_access_token(credentials.credentials)
    except InvalidTokenError:
        raise _unauthorized("Invalid or expired token") from None
    user = await session.get(User, claims.user_id)
    if user is None or not user.is_active:
        raise _unauthorized("Invalid or expired token")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_role(*roles: UserRole) -> Callable[[User], Awaitable[User]]:
    """Dependency factory: allow only users holding one of `roles`."""
    allowed = frozenset(roles)

    async def dependency(user: CurrentUser) -> User:
        if user.role not in allowed:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
        return user

    return dependency


def client_ip(request: Request) -> str:
    """Peer address. Behind a reverse proxy, run Uvicorn with --proxy-headers and
    --forwarded-allow-ips so this reflects the real client, never a header the
    client controls directly."""
    return request.client.host if request.client else "unknown"


async def enforce_rate_limit(redis: Redis, scope: str, identifier: str, spec: str) -> None:
    """Raise 429 over the limit. Fails closed (503) if Redis is unavailable: an
    unprotected login endpoint is worse than a briefly unavailable one."""
    try:
        result = await hit(redis, scope, identifier, RateLimitRule.parse(spec))
    except RedisError:
        logger.exception("Rate limiter unavailable (scope=%s)", scope)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Service unavailable"
        ) from None
    if not result.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests",
            headers={"Retry-After": str(result.retry_after_seconds)},
        )


def rate_limit_by_ip(scope: str, spec: Callable[[Settings], str]) -> Callable[..., Awaitable[None]]:
    """Dependency factory: limit an endpoint per client IP."""

    async def dependency(request: Request, redis: RedisClient) -> None:
        await enforce_rate_limit(redis, scope, client_ip(request), spec(get_settings()))

    return dependency
