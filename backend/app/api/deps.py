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
from app.models import Machine, User, UserRole
from app.services.machine_auth import (
    HEADER_MACHINE,
    HEADER_NONCE,
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
    MachineAuthError,
    parse_signed_headers,
    verify_machine_request,
)
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


async def enforce_rate_limit(
    redis: Redis, scope: str, identifier: str, spec: str, *, fail_open: bool = False
) -> None:
    """Raise 429 over the limit.

    If Redis is unavailable, fail closed (503) by default: an unprotected login
    endpoint is worse than a briefly unavailable one. Read-only public endpoints
    pass `fail_open=True` so the map keeps working during a Redis outage.
    """
    try:
        result = await hit(redis, scope, identifier, RateLimitRule.parse(spec))
    except RedisError:
        logger.exception("Rate limiter unavailable (scope=%s)", scope)
        if fail_open:
            return
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Service unavailable"
        ) from None
    if not result.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests",
            headers={"Retry-After": str(result.retry_after_seconds)},
        )


def rate_limit_by_ip(
    scope: str, spec: Callable[[Settings], str], *, fail_open: bool = False
) -> Callable[..., Awaitable[None]]:
    """Dependency factory: limit an endpoint per client IP."""

    async def dependency(request: Request, redis: RedisClient) -> None:
        await enforce_rate_limit(
            redis, scope, client_ip(request), spec(get_settings()), fail_open=fail_open
        )

    return dependency


async def get_authenticated_machine(
    request: Request, session: DbSession, redis: RedisClient
) -> Machine:
    """Authenticate a machine from its Ed25519-signed request (ADR-010).

    Signs over the raw path and query exactly as received, so no normalisation
    difference between client and server can break or bypass verification.
    Every failure gets the same 401; the reason is only logged.
    """
    raw_path = request.scope.get("raw_path") or request.scope["path"].encode()
    query = request.scope.get("query_string", b"")
    path_and_query = raw_path.decode("latin-1") + ("?" + query.decode("latin-1") if query else "")
    try:
        signed = parse_signed_headers(
            {
                name: request.headers.get(name)
                for name in (HEADER_MACHINE, HEADER_TIMESTAMP, HEADER_NONCE, HEADER_SIGNATURE)
            }
        )
        return await verify_machine_request(
            session,
            redis,
            headers=signed,
            method=request.method,
            path_and_query=path_and_query,
            body=await request.body(),
            max_skew_seconds=get_settings().machine_auth_max_skew_seconds,
        )
    except MachineAuthError as exc:
        logger.warning(
            "Machine authentication rejected: %s (serial header=%r)",
            exc.reason,
            request.headers.get(HEADER_MACHINE),
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid machine signature"
        ) from None
    except RedisError:
        logger.exception("Replay store unavailable")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Service unavailable"
        ) from None


AuthenticatedMachine = Annotated[Machine, Depends(get_authenticated_machine)]
