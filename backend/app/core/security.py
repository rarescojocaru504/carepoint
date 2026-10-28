import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from app.core.config import get_settings
from app.models.enums import UserRole

# argon2id with the library's RFC 9106 "low memory" defaults (64 MiB, t=3, p=4).
_hasher = PasswordHasher()

# Verified against when the email is unknown, so "no such user" and "wrong
# password" take the same time and cannot be told apart.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))

JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_TYPE = "access"  # noqa: S105  (a token *type* label, not a secret)


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """Constant-time check; never raises for a wrong password or a malformed hash."""
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def verify_dummy_password(password: str) -> None:
    """Burn the same CPU time as a real verification (see _DUMMY_HASH)."""
    verify_password(_DUMMY_HASH, password)


def password_needs_rehash(password_hash: str) -> bool:
    """True when the hash was made with weaker parameters than the current ones."""
    return _hasher.check_needs_rehash(password_hash)


# --- access tokens (JWT) -------------------------------------------------------


@dataclass(frozen=True)
class AccessTokenClaims:
    user_id: uuid.UUID
    role: UserRole
    expires_at: datetime


class InvalidTokenError(Exception):
    pass


def create_access_token(user_id: uuid.UUID, role: UserRole) -> tuple[str, int]:
    """Return (token, lifetime in seconds)."""
    settings = get_settings()
    now = datetime.now(UTC)
    ttl = settings.access_token_ttl_seconds
    payload = {
        "sub": str(user_id),
        "role": role.value,
        "type": ACCESS_TOKEN_TYPE,
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": now,
        "nbf": now,
        "exp": now + timedelta(seconds=ttl),
        "jti": uuid.uuid4().hex,
    }
    token = jwt.encode(payload, settings.jwt_secret.get_secret_value(), algorithm=JWT_ALGORITHM)
    return token, ttl


def decode_access_token(token: str) -> AccessTokenClaims:
    """Validate signature, algorithm, expiry, issuer, audience and token type.

    The algorithm is pinned: tokens claiming `none` or an asymmetric algorithm
    (classic algorithm-confusion attacks) are rejected.
    """
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[JWT_ALGORITHM],
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
            options={"require": ["sub", "exp", "iat", "nbf", "iss", "aud", "type"]},
            leeway=5,
        )
        if payload["type"] != ACCESS_TOKEN_TYPE:
            raise InvalidTokenError("not an access token")
        return AccessTokenClaims(
            user_id=uuid.UUID(payload["sub"]),
            role=UserRole(payload["role"]),
            expires_at=datetime.fromtimestamp(payload["exp"], UTC),
        )
    except (jwt.PyJWTError, KeyError, ValueError) as exc:
        raise InvalidTokenError(str(exc)) from exc


# --- refresh tokens (opaque) -----------------------------------------------------


def generate_refresh_token() -> str:
    """256 bits of randomness; only its SHA-256 is ever stored."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """SHA-256 is appropriate here (unlike for passwords): the input is a
    high-entropy random value, so it cannot be brute-forced from the hash."""
    return hashlib.sha256(token.encode()).hexdigest()
