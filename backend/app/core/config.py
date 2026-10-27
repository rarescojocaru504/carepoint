from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings, loaded from environment variables (or a `.env` file)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    project_name: str = "CarePoint API"
    api_v1_prefix: str = "/api/v1"

    database_url: str = Field(
        default="postgresql+asyncpg://carepoint:carepoint@localhost:5432/carepoint",
        description="Async SQLAlchemy DSN (must use the asyncpg driver).",
    )
    redis_url: str = "redis://localhost:6379/0"
    db_echo: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
