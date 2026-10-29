from functools import lru_cache
from typing import Annotated, Literal, Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Development-only default; production refuses to start with it (see validator).
DEV_JWT_SECRET = "dev-only-insecure-jwt-secret-change-me-in-production"  # noqa: S105
DEV_PICKUP_CODE_PEPPER = "dev-only-insecure-pickup-pepper-change-me"

# "<max requests>/<window seconds>", e.g. "5/60".
RateLimitSpec = Annotated[str, Field(pattern=r"^[1-9][0-9]*/[1-9][0-9]*$")]


class Settings(BaseSettings):
    """Application settings, loaded from environment variables (or a `.env` file)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: Literal["development", "test", "production"] = "development"

    # --- observability (ADR-029) -----------------------------------------------------
    # "json": one JSON object per line, for log collectors; "text" for reading locally.
    log_format: Literal["json", "text"] = "json"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    # Bearer token a metrics scraper sends to GET /metrics. Unset: only signed-in
    # operators and admins can read the metrics.
    metrics_token: SecretStr | None = None
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

    # Generous: a whole campus can share one NAT address (ADR-014).
    rate_limit_discovery_ip: RateLimitSpec = "1200/60"

    # A machine counts as online if it was heard from within this window.
    machine_online_window_seconds: int = Field(default=90, ge=10, le=3600)

    # --- reservations (ADR-012) --------------------------------------------------
    # How long a reservation holds stock. 900 s in production; set ~60 for live
    # demos so an expiry can be shown on stage.
    reservation_hold_seconds: int = Field(default=900, ge=30, le=3600)
    max_active_reservations_per_user: int = Field(default=3, ge=1, le=20)
    rate_limit_reservation_user: RateLimitSpec = "20/60"
    # Checkout attempts per user: blunts card-testing with stolen card numbers.
    rate_limit_checkout_user: RateLimitSpec = "10/60"

    # --- pickup codes (ADR-016) -----------------------------------------------------
    # Server-side key for hashing pickup codes; a database leak alone cannot
    # recover codes from their hashes without it.
    pickup_code_pepper: SecretStr = SecretStr(DEV_PICKUP_CODE_PEPPER)
    # Wrong codes tolerated per machine per window before it must wait.
    rate_limit_pickup_failures_machine: RateLimitSpec = "20/60"
    rate_limit_pickup_rotate_user: RateLimitSpec = "5/60"

    # --- customer live notices (ADR-026) --------------------------------------------
    # Open GET /api/v1/events streams per user. Each holds a Redis connection, so
    # one account can't open hundreds; 3 leaves room for a reconnect overlap.
    customer_event_streams_per_user: int = Field(default=3, ge=1, le=20)

    # Background jobs (ADR-013). Disabled in the test suite, which drives them directly.
    background_workers_enabled: bool = True
    expiry_sweep_interval_seconds: int = Field(default=15, ge=1, le=600)
    reconcile_interval_seconds: int = Field(default=300, ge=10, le=86_400)
    # A Redis hold with no Postgres row is kept this long (its insert may still
    # be in flight) before reconciliation treats it as an orphan.
    hold_orphan_grace_seconds: int = Field(default=60, ge=5, le=3600)

    # Machine request signing (ADR-010): accepted clock skew, either direction.
    machine_auth_max_skew_seconds: int = Field(default=60, ge=5, le=600)

    # --- machine gateway (ADR-017) ------------------------------------------------
    # Told to machines in `welcome`; well inside the online window, so a machine
    # stays online across one lost heartbeat. A connection silent for the whole
    # online window is closed.
    machine_heartbeat_interval_seconds: int = Field(default=30, ge=5, le=600)
    # Messages per connection per window; a flood closes the connection.
    machine_ws_rate_limit: RateLimitSpec = "30/10"
    # Malformed or rejected messages tolerated per connection before it is closed.
    machine_ws_max_invalid_messages: int = Field(default=10, ge=1, le=1000)
    # Once a pickup code is accepted, the hold lasts at least this much longer,
    # so the expiry can never overtake a dispense in progress (ADR-018).
    dispense_timeout_seconds: int = Field(default=120, ge=10, le=900)

    @model_validator(mode="after")
    def _heartbeat_inside_online_window(self) -> Self:
        if self.machine_heartbeat_interval_seconds * 2 > self.machine_online_window_seconds:
            raise ValueError(
                "MACHINE_HEARTBEAT_INTERVAL_SECONDS must be at most half of "
                "MACHINE_ONLINE_WINDOW_SECONDS"
            )
        return self

    @model_validator(mode="after")
    def _production_requires_real_secrets(self) -> Self:
        if self.environment == "production":
            secret = self.jwt_secret.get_secret_value()
            if secret == DEV_JWT_SECRET or len(secret) < 32:
                raise ValueError("JWT_SECRET must be set to >= 32 random characters in production")
            pepper = self.pickup_code_pepper.get_secret_value()
            if pepper == DEV_PICKUP_CODE_PEPPER or len(pepper) < 32:
                raise ValueError(
                    "PICKUP_CODE_PEPPER must be set to >= 32 random characters in production"
                )
            if self.metrics_token is not None and len(self.metrics_token.get_secret_value()) < 32:
                raise ValueError("METRICS_TOKEN must be >= 32 random characters in production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
