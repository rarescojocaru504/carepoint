import asyncio

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.helpers import alembic_config

pytestmark = pytest.mark.integration


async def _scalar(database_url: str, sql: str) -> object:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as conn:
            return (await conn.execute(text(sql))).scalar_one()
    finally:
        await engine.dispose()


def _postgis_installed(database_url: str) -> bool:
    count = asyncio.run(
        _scalar(database_url, "SELECT count(*) FROM pg_extension WHERE extname = 'postgis'")
    )
    return count == 1


def _app_table_count(database_url: str) -> object:
    return asyncio.run(
        _scalar(
            database_url,
            "SELECT count(*) FROM pg_tables WHERE schemaname = 'public' "
            "AND tablename NOT IN ('spatial_ref_sys', 'alembic_version')",
        )
    )


def test_migrations_upgrade_and_downgrade(test_database_url: str) -> None:
    config = alembic_config(test_database_url)
    command.downgrade(config, "base")

    command.upgrade(config, "head")
    assert _postgis_installed(test_database_url)
    assert _app_table_count(test_database_url) == 7

    command.downgrade(config, "base")
    assert not _postgis_installed(test_database_url)
    assert _app_table_count(test_database_url) == 0

    command.upgrade(config, "head")


def test_models_match_migrations(test_database_url: str) -> None:
    """Fails if a model changed without a matching Alembic revision."""
    config = alembic_config(test_database_url)
    command.upgrade(config, "head")
    command.check(config)
