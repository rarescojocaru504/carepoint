"""Authentication & authorisation, mostly from an attacker's point of view."""

import asyncio
import base64
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import jwt
import pytest
from pydantic import SecretStr, ValidationError
from redis.asyncio import Redis
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import Settings, get_settings
from app.core.db import get_db
from app.core.redis import get_redis
from app.core.security import JWT_ALGORITHM, hash_password, hash_token
from app.main import app
from app.models import RefreshToken, User, UserRole
from app.services import auth as auth_service
from tests.factories import make_user

pytestmark = pytest.mark.integration

PASSWORD = "correct horse battery staple"


async def _user(session: AsyncSession, **overrides: Any) -> User:
    return await make_user(session, password_hash=hash_password(PASSWORD), **overrides)


async def _login(api: httpx.AsyncClient, email: str, password: str = PASSWORD) -> httpx.Response:
    return await api.post("/api/v1/auth/login", json={"email": email, "password": password})


async def _tokens(api: httpx.AsyncClient, user: User) -> dict[str, Any]:
    response = await _login(api, user.email)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _forge(claims: dict[str, Any], *, secret: str | None = None, alg: str = JWT_ALGORITHM) -> str:
    settings = get_settings()
    now = datetime.now(UTC)
    payload = {
        "type": "access",
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": now,
        "nbf": now,
        "exp": now + timedelta(minutes=5),
    } | claims
    key = secret if secret is not None else settings.jwt_secret.get_secret_value()
    return jwt.encode(payload, key, algorithm=alg)


# --- registration ----------------------------------------------------------------


async def test_register_creates_customer(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    response = await api.post(
        "/api/v1/auth/register",
        json={"email": "New.User@Example.com", "password": PASSWORD, "full_name": "New User"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["email"] == "new.user@example.com"
    assert body["role"] == "customer"
    assert "password" not in json.dumps(body) and "hash" not in json.dumps(body)
    stored = await db_session.scalar(select(User).where(User.email == "new.user@example.com"))
    assert stored is not None and stored.password_hash.startswith("$argon2id$")


async def test_register_rejects_role_escalation(api: httpx.AsyncClient) -> None:
    response = await api.post(
        "/api/v1/auth/register",
        json={"email": "x@example.com", "password": PASSWORD, "full_name": "X", "role": "admin"},
    )
    assert response.status_code == 422


async def test_register_duplicate_email_case_insensitive(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await _user(db_session, email="taken@example.com")
    response = await api.post(
        "/api/v1/auth/register",
        json={"email": "TAKEN@example.com", "password": PASSWORD, "full_name": "Dup"},
    )
    assert response.status_code == 409


# --- login & /me -----------------------------------------------------------------


async def test_login_and_me(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    user = await _user(db_session, email="ana@example.com")
    response = await _login(api, "ANA@example.com")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    tokens = response.json()
    assert tokens["token_type"] == "bearer"
    assert tokens["expires_in"] == get_settings().access_token_ttl_seconds

    me = await api.get("/api/v1/me", headers=_bearer(tokens["access_token"]))
    assert me.status_code == 200
    assert me.json()["id"] == str(user.id)


async def test_login_failures_are_indistinguishable(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await _user(db_session, email="real@example.com")
    await _user(db_session, email="disabled@example.com", is_active=False)
    responses = [
        await _login(api, "real@example.com", "wrong password!"),
        await _login(api, "nobody@example.com"),
        await _login(api, "disabled@example.com"),
    ]
    assert {r.status_code for r in responses} == {401}
    assert len({r.text for r in responses}) == 1  # no account enumeration


async def test_me_requires_token(api: httpx.AsyncClient) -> None:
    response = await api.get("/api/v1/me")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "make_token",
    [
        pytest.param(lambda uid: _forge({"sub": uid, "role": "customer"}, secret="x" * 40),
                     id="wrong-signature"),
        pytest.param(lambda uid: _forge({"sub": uid, "role": "customer",
                                         "exp": datetime.now(UTC) - timedelta(minutes=1)}),
                     id="expired"),
        pytest.param(lambda uid: _forge({"sub": uid, "role": "customer", "aud": "other-api"}),
                     id="wrong-audience"),
        pytest.param(lambda uid: _forge({"sub": uid, "role": "customer", "iss": "evil"}),
                     id="wrong-issuer"),
        pytest.param(lambda uid: _forge({"sub": uid, "role": "customer", "type": "refresh"}),
                     id="wrong-type"),
        pytest.param(lambda uid: _forge({"sub": uid, "role": "customer"}, secret="", alg="none"),
                     id="alg-none"),
        pytest.param(lambda uid: _forge({"sub": str(uuid.uuid4()), "role": "customer"}),
                     id="unknown-user"),
        pytest.param(lambda uid: "not.a.jwt", id="garbage"),
    ],
)  # fmt: skip
async def test_me_rejects_bad_tokens(
    api: httpx.AsyncClient, db_session: AsyncSession, make_token: Any
) -> None:
    user = await _user(db_session)
    response = await api.get("/api/v1/me", headers=_bearer(make_token(str(user.id))))
    assert response.status_code == 401


async def test_alg_none_without_signature_rejected(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """Hand-built `alg: none` token with an empty signature segment."""
    user = await _user(db_session)

    def b64(data: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

    settings = get_settings()
    exp = int((datetime.now(UTC) + timedelta(minutes=5)).timestamp())
    token = (
        b64({"alg": "none", "typ": "JWT"})
        + "."
        + b64({"sub": str(user.id), "role": "admin", "type": "access", "exp": exp,
               "iat": exp - 300, "nbf": exp - 300,
               "iss": settings.jwt_issuer, "aud": settings.jwt_audience})
        + "."
    )  # fmt: skip
    assert (await api.get("/api/v1/me", headers=_bearer(token))).status_code == 401


async def test_deactivated_user_locked_out_immediately(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    user = await _user(db_session)
    tokens = await _tokens(api, user)
    user.is_active = False
    await db_session.flush()
    response = await api.get("/api/v1/me", headers=_bearer(tokens["access_token"]))
    assert response.status_code == 401


# --- roles -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "expected"),
    [(UserRole.CUSTOMER, 403), (UserRole.OPERATOR, 403), (UserRole.ADMIN, 200)],
)
async def test_admin_users_requires_admin(
    api: httpx.AsyncClient, db_session: AsyncSession, role: UserRole, expected: int
) -> None:
    user = await _user(db_session, role=role)
    tokens = await _tokens(api, user)
    response = await api.get("/api/v1/admin/users", headers=_bearer(tokens["access_token"]))
    assert response.status_code == expected
    if expected == 200:
        assert any(u["id"] == str(user.id) for u in response.json())


async def test_admin_requires_authentication(api: httpx.AsyncClient) -> None:
    assert (await api.get("/api/v1/admin/users")).status_code == 401


async def test_role_comes_from_database_not_token(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """An admin demoted after login loses admin access with the same token."""
    user = await _user(db_session, role=UserRole.ADMIN)
    tokens = await _tokens(api, user)
    user.role = UserRole.CUSTOMER
    await db_session.flush()
    response = await api.get("/api/v1/admin/users", headers=_bearer(tokens["access_token"]))
    assert response.status_code == 403


async def test_forged_admin_claim_does_not_grant_admin(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    customer = await _user(db_session, role=UserRole.CUSTOMER)
    token = _forge({"sub": str(customer.id), "role": "admin"})  # validly signed, lies about role
    response = await api.get("/api/v1/admin/users", headers=_bearer(token))
    assert response.status_code == 403


# --- refresh rotation, reuse detection, logout ----------------------------------


async def test_refresh_rotates_tokens(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    user = await _user(db_session)
    first = await _tokens(api, user)
    response = await api.post(
        "/api/v1/auth/refresh", json={"refresh_token": first["refresh_token"]}
    )
    assert response.status_code == 200
    second = response.json()
    assert second["refresh_token"] != first["refresh_token"]
    me = await api.get("/api/v1/me", headers=_bearer(second["access_token"]))
    assert me.status_code == 200


async def test_refresh_reuse_revokes_whole_family(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    user = await _user(db_session)
    stolen = (await _tokens(api, user))["refresh_token"]
    legit = (await api.post("/api/v1/auth/refresh", json={"refresh_token": stolen})).json()

    # Attacker replays the already-rotated token.
    replay = await api.post("/api/v1/auth/refresh", json={"refresh_token": stolen})
    assert replay.status_code == 401
    # ...which also kills the legitimate client's current token (family revoked).
    after = await api.post("/api/v1/auth/refresh", json={"refresh_token": legit["refresh_token"]})
    assert after.status_code == 401


async def test_reuse_does_not_affect_other_logins(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    user = await _user(db_session)
    phone = await _tokens(api, user)
    laptop = await _tokens(api, user)
    await api.post("/api/v1/auth/refresh", json={"refresh_token": phone["refresh_token"]})
    await api.post("/api/v1/auth/refresh", json={"refresh_token": phone["refresh_token"]})
    still_ok = await api.post(
        "/api/v1/auth/refresh", json={"refresh_token": laptop["refresh_token"]}
    )
    assert still_ok.status_code == 200


async def test_refresh_rejects_expired_and_unknown(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    user = await _user(db_session)
    tokens = await _tokens(api, user)
    await db_session.execute(
        update(RefreshToken)
        .where(RefreshToken.token_hash == hash_token(tokens["refresh_token"]))
        .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    expired = await api.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert expired.status_code == 401
    unknown = await api.post("/api/v1/auth/refresh", json={"refresh_token": "A" * 43})
    assert unknown.status_code == 401


async def test_refresh_stores_only_hashes(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    user = await _user(db_session)
    tokens = await _tokens(api, user)
    stored = list(await db_session.scalars(select(RefreshToken.token_hash)))
    assert tokens["refresh_token"] not in stored
    assert hash_token(tokens["refresh_token"]) in stored


async def test_logout_revokes_login(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    user = await _user(db_session)
    tokens = await _tokens(api, user)
    logout = await api.post("/api/v1/auth/logout", json={"refresh_token": tokens["refresh_token"]})
    assert logout.status_code == 204
    again = await api.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert again.status_code == 401
    unknown = await api.post("/api/v1/auth/logout", json={"refresh_token": "B" * 43})
    assert unknown.status_code == 204  # reveals nothing


async def test_concurrent_refresh_with_same_token(migrated_database_url: str) -> None:
    """Two simultaneous refreshes of one token: at most one wins, and the race is
    treated as reuse, so the family (including the winner's new token) is revoked.

    Uses real committed rows on separate connections (a rolled-back shared
    session cannot run two transactions at once), cleaned up afterwards.
    """
    engine = create_async_engine(migrated_database_url, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    email = f"race-{uuid.uuid4().hex[:8]}@example.com"
    try:
        async with sessions() as setup:
            user = User(email=email, password_hash="!", full_name="Race")
            setup.add(user)
            await setup.commit()
            pair = await auth_service.issue_token_pair(setup, user)

        async def attempt() -> str:
            async with sessions() as session:
                try:
                    await auth_service.rotate_refresh_token(session, pair.refresh_token)
                    return "ok"
                except auth_service.InvalidRefreshTokenError:
                    return "rejected"

        results = await asyncio.gather(attempt(), attempt())
        assert sorted(results) == ["ok", "rejected"]

        async with sessions() as check:
            live = await check.scalars(
                select(RefreshToken).where(
                    RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None)
                )
            )
            assert list(live) == []
    finally:
        async with sessions() as cleanup:
            user_id = select(User.id).where(User.email == email).scalar_subquery()
            await cleanup.execute(delete(RefreshToken).where(RefreshToken.user_id == user_id))
            await cleanup.execute(delete(User).where(User.email == email))
            await cleanup.commit()
        await engine.dispose()


# --- rate limiting ---------------------------------------------------------------


async def test_login_rate_limited_per_email(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await _user(db_session, email="target@example.com")
    limit = int(get_settings().rate_limit_login_email.split("/")[0])
    statuses = [
        (await _login(api, "target@example.com", "guess-attempt")).status_code
        for _ in range(limit + 1)
    ]
    assert statuses[:limit] == [401] * limit
    assert statuses[limit] == 429
    # Even the right password is refused while limited.
    blocked = await _login(api, "target@example.com")
    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) > 0
    # Another account from the same IP is unaffected.
    other = await _user(db_session, email="other@example.com")
    assert (await _login(api, other.email)).status_code == 200


async def test_login_rate_limited_per_ip(
    api: httpx.AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "rate_limit_login_ip", "3/60")
    statuses = [(await _login(api, f"u{i}@example.com")).status_code for i in range(4)]
    assert statuses == [401, 401, 401, 429]


async def test_register_rate_limited_per_ip(
    api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "rate_limit_register_ip", "2/3600")
    statuses = [
        (
            await api.post(
                "/api/v1/auth/register",
                json={"email": f"r{i}@example.com", "password": PASSWORD, "full_name": "R"},
            )
        ).status_code
        for i in range(3)
    ]
    assert statuses == [201, 201, 429]


async def test_rate_limit_keys_hash_identifiers(
    api: httpx.AsyncClient, redis: Redis, db_session: AsyncSession
) -> None:
    await _login(api, "private.person@example.com", "whatever-pw")
    keys = await redis.keys("ratelimit:*")
    assert keys
    assert not any("private.person" in key or "127.0.0.1" in key for key in keys)
    assert all(ttl > 0 for ttl in [await redis.ttl(key) for key in keys])


async def test_rate_limiter_fails_closed(db_session: AsyncSession) -> None:
    """If Redis is down, login is refused (503) rather than left unprotected."""
    dead: Redis = Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.2)

    async def _db() -> Any:
        yield db_session

    async def _redis() -> Redis:
        return dead

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_redis] = _redis
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await _login(client, "a@example.com")
        assert response.status_code == 503
    finally:
        app.dependency_overrides.clear()
        await dead.aclose()


# --- configuration ---------------------------------------------------------------


def test_production_refuses_default_or_short_jwt_secret() -> None:
    with pytest.raises(ValidationError, match="JWT_SECRET"):
        Settings(environment="production")
    with pytest.raises(ValidationError, match="JWT_SECRET"):
        Settings(environment="production", jwt_secret=SecretStr("too-short"))
    Settings(
        environment="production",
        jwt_secret=SecretStr("x" * 40),
        pickup_code_pepper=SecretStr("y" * 40),
    )


def test_rate_limit_spec_validated() -> None:
    with pytest.raises(ValidationError):
        Settings(rate_limit_login_ip="ten per minute")
