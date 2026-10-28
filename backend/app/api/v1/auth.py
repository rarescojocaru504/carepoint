from fastapi import APIRouter, Depends, HTTPException, Response, status

from app.api.deps import DbSession, RedisClient, enforce_rate_limit, rate_limit_by_ip
from app.core.config import get_settings
from app.schemas.auth import LoginRequest, RefreshRequest, TokenPair
from app.schemas.user import UserCreate, UserRead
from app.services import auth as auth_service

router = APIRouter(prefix="/auth", tags=["auth"])

_INVALID_CREDENTIALS = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Invalid email or password",
    headers={"WWW-Authenticate": "Bearer"},
)
_INVALID_REFRESH = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired refresh token"
)


def _no_store(response: Response) -> None:
    # Token responses must never be cached by browsers or proxies (RFC 6749 §5.1).
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


@router.post(
    "/register",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limit_by_ip("register-ip", lambda s: s.rate_limit_register_ip))],
    responses={409: {"description": "Email already registered"}},
)
async def register(data: UserCreate, session: DbSession) -> UserRead:
    try:
        user = await auth_service.register_user(session, data)
    except auth_service.EmailAlreadyRegisteredError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Email already registered"
        ) from None
    return UserRead.model_validate(user)


@router.post(
    "/login",
    response_model=TokenPair,
    dependencies=[Depends(rate_limit_by_ip("login-ip", lambda s: s.rate_limit_login_ip))],
    responses={401: {"description": "Invalid credentials"}, 429: {"description": "Rate limited"}},
)
async def login(
    data: LoginRequest, session: DbSession, redis: RedisClient, response: Response
) -> TokenPair:
    # Per-account limit as well as per-IP: stops slow distributed guessing
    # against one account and fast guessing from one address.
    await enforce_rate_limit(
        redis, "login-email", data.email, get_settings().rate_limit_login_email
    )
    try:
        user = await auth_service.authenticate(session, data.email, data.password)
    except auth_service.InvalidCredentialsError:
        raise _INVALID_CREDENTIALS from None
    _no_store(response)
    return await auth_service.issue_token_pair(session, user)


@router.post(
    "/refresh",
    response_model=TokenPair,
    dependencies=[Depends(rate_limit_by_ip("refresh-ip", lambda s: s.rate_limit_refresh_ip))],
    responses={401: {"description": "Invalid, expired, revoked or reused token"}},
)
async def refresh(data: RefreshRequest, session: DbSession, response: Response) -> TokenPair:
    try:
        pair = await auth_service.rotate_refresh_token(session, data.refresh_token)
    except auth_service.InvalidRefreshTokenError:
        raise _INVALID_REFRESH from None
    _no_store(response)
    return pair


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(data: RefreshRequest, session: DbSession) -> None:
    await auth_service.logout(session, data.refresh_token)
