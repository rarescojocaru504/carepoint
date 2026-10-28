"""User registration, login and refresh-token rotation."""

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.core.config import get_settings
from app.core.security import (
    create_access_token,
    generate_refresh_token,
    hash_password,
    hash_token,
    password_needs_rehash,
    verify_dummy_password,
    verify_password,
)
from app.models import RefreshToken, User, UserRole
from app.schemas.auth import TokenPair
from app.schemas.user import UserCreate

logger = logging.getLogger(__name__)


class AuthError(Exception):
    """Base class; messages are safe to show to clients."""


class EmailAlreadyRegisteredError(AuthError):
    pass


class InvalidCredentialsError(AuthError):
    pass


class InvalidRefreshTokenError(AuthError):
    pass


async def register_user(session: AsyncSession, data: UserCreate) -> User:
    """Create a customer account. The role is never taken from input."""
    if await session.scalar(select(User.id).where(User.email == data.email)):
        raise EmailAlreadyRegisteredError
    user = User(
        email=data.email,
        full_name=data.full_name,
        role=UserRole.CUSTOMER,
        # argon2 is deliberately slow; keep it off the event loop.
        password_hash=await run_in_threadpool(hash_password, data.password),
    )
    session.add(user)
    try:
        await session.commit()
    except IntegrityError as exc:
        # Lost a race with a concurrent sign-up for the same email.
        await session.rollback()
        raise EmailAlreadyRegisteredError from exc
    return user


async def authenticate(session: AsyncSession, email: str, password: str) -> User:
    """Return the active user for these credentials or raise InvalidCredentialsError.

    Unknown email, wrong password and disabled account all fail identically and
    in the same time, so responses cannot be used to enumerate accounts.
    """
    user = await session.scalar(select(User).where(User.email == email))
    if user is None:
        await run_in_threadpool(verify_dummy_password, password)
        raise InvalidCredentialsError
    if not await run_in_threadpool(verify_password, user.password_hash, password):
        raise InvalidCredentialsError
    if not user.is_active:
        raise InvalidCredentialsError
    if password_needs_rehash(user.password_hash):
        user.password_hash = await run_in_threadpool(hash_password, password)
        await session.commit()
    return user


async def issue_token_pair(
    session: AsyncSession, user: User, family_id: uuid.UUID | None = None
) -> TokenPair:
    """Mint an access token and a new refresh token (new family = new login)."""
    access_token, expires_in = create_access_token(user.id, user.role)
    refresh_token = generate_refresh_token()
    session.add(
        RefreshToken(
            user_id=user.id,
            family_id=family_id or uuid.uuid4(),
            token_hash=hash_token(refresh_token),
            expires_at=datetime.now(UTC)
            + timedelta(seconds=get_settings().refresh_token_ttl_seconds),
        )
    )
    await session.commit()
    return TokenPair(access_token=access_token, refresh_token=refresh_token, expires_in=expires_in)


async def rotate_refresh_token(session: AsyncSession, presented: str) -> TokenPair:
    """Exchange a refresh token for a new pair, revoking the presented one.

    Reuse of an already-rotated token means it was stolen (either the thief or
    the legitimate client is replaying it): revoke the entire family, logging
    every device of that login out, and refuse.
    """
    now = datetime.now(UTC)
    # Row lock: two concurrent refreshes with the same token serialise, and the
    # second one is treated as reuse.
    token = await session.scalar(
        select(RefreshToken)
        .where(RefreshToken.token_hash == hash_token(presented))
        .with_for_update()
    )
    if token is None:
        raise InvalidRefreshTokenError
    if token.revoked_at is not None:
        logger.warning(
            "Refresh token reuse detected; revoking family",
            extra={"user_id": str(token.user_id), "family_id": str(token.family_id)},
        )
        await revoke_family(session, token.family_id)
        await session.commit()
        raise InvalidRefreshTokenError
    if token.expires_at <= now:
        raise InvalidRefreshTokenError

    user = await session.get(User, token.user_id)
    if user is None or not user.is_active:
        await revoke_family(session, token.family_id)
        await session.commit()
        raise InvalidRefreshTokenError

    token.revoked_at = now
    return await issue_token_pair(session, user, family_id=token.family_id)


async def revoke_family(session: AsyncSession, family_id: uuid.UUID) -> None:
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )


async def logout(session: AsyncSession, presented: str) -> None:
    """Revoke the login (token family) the presented token belongs to.

    Idempotent and silent for unknown tokens, so it reveals nothing.
    """
    family_id = await session.scalar(
        select(RefreshToken.family_id).where(RefreshToken.token_hash == hash_token(presented))
    )
    if family_id is not None:
        await revoke_family(session, family_id)
        await session.commit()
