import asyncio
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

BACKEND_DIR = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.integration


def _alembic_config() -> Config:
    # No config file name: avoids alembic's fileConfig() hijacking pytest logging.
    config = Config()
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    return config


async def _postgis_installed(database_url: str) -> bool:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT count(*) FROM pg_extension WHERE extname = 'postgis'")
            )
            count: int = result.scalar_one()
            return count == 1
    finally:
        await engine.dispose()


def test_migrations_upgrade_and_downgrade(test_database_url: str) -> None:
    config = _alembic_config()
    command.downgrade(config, "base")

    command.upgrade(config, "head")
    assert asyncio.run(_postgis_installed(test_database_url))

    command.downgrade(config, "base")
    assert not asyncio.run(_postgis_installed(test_database_url))

    command.upgrade(config, "head")


def test_models_match_migrations(test_database_url: str) -> None:
    """Fails if a model changed without a matching Alembic revision."""
    config = _alembic_config()
    command.upgrade(config, "head")
    command.check(config)
