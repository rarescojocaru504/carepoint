from functools import lru_cache
from typing import Annotated, Literal, Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Development-only default; production refuses to start with it (see validator).
DEV_JWT_SECRET = "dev-only-insecure-jwt-secret-change-me-in-production"  # noqa: S105

# "<max requests>/<window seconds>", e.g. "5/60".
RateLimitSpec = Annotated[str, Field(pattern=r"^[1-9][0-9]*/[1-9][0-9]*$")]


class Settings(BaseSettings):
    """Application settings, loaded from environment variables (or a `.env` file)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: Literal["development", "test", "production"] = "development"
    project_name: str = "CarePoint API"
    api_v1_prefix: str = "/api/v1"
    currency: str = Field(default="RON", pattern=r"^[A-Z]{3}$")

    database_url: str = Field(
        default="postgresql+asyncpg://carepoint:carepoint@localhost:5432/carepoint",
        description="Async SQLAlchemy DSN (must use the asyncpg driver).",
    )
    redis_url: str = "redis://localhost:6379/0"
    db_echo: bool = False

    # --- auth -------------------------------------------------------------------
    jwt_secret: SecretStr = SecretStr(DEV_JWT_SECRET)
    jwt_issuer: str = "carepoint"
    jwt_audience: str = "carepoint-api"
    access_token_ttl_seconds: int = Field(default=900, gt=0, le=3600)
    refresh_token_ttl_seconds: int = Field(default=30 * 24 * 3600, gt=0)

    # Rate limits as "<requests>/<window seconds>". Per-IP limits are generous
    # because a whole campus can sit behind one NAT address.
    rate_limit_login_ip: RateLimitSpec = "30/60"
    rate_limit_login_email: RateLimitSpec = "5/60"
    rate_limit_register_ip: RateLimitSpec = "20/3600"
    rate_limit_refresh_ip: RateLimitSpec = "60/60"

    @model_validator(mode="after")
    def _production_requires_real_secrets(self) -> Self:
        if self.environment == "production":
            secret = self.jwt_secret.get_secret_value()
            if secret == DEV_JWT_SECRET or len(secret) < 32:
                raise ValueError("JWT_SECRET must be set to >= 32 random characters in production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
