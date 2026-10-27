import asyncio
from logging.config import fileConfig
from typing import Literal

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.schema import SchemaItem

import app.models  # noqa: F401  (registers every model on Base.metadata)
from app.core.config import get_settings
from app.core.db import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# The DSN always comes from application settings (DATABASE_URL), never alembic.ini.
database_url = get_settings().database_url

# Tables owned by the PostGIS extension (and its optional tiger/topology add-ons).
# They are not described by our models, so autogenerate must not try to drop them.
POSTGIS_SCHEMAS = frozenset({"tiger", "tiger_data", "topology"})
POSTGIS_TABLES = frozenset({"spatial_ref_sys"})


def include_object(
    obj: SchemaItem,
    name: str | None,
    type_: Literal[
        "schema", "table", "column", "index", "unique_constraint", "foreign_key_constraint"
    ],
    reflected: bool,
    compare_to: SchemaItem | None,
) -> bool:
    if type_ == "table" and reflected and compare_to is None:
        schema = getattr(obj, "schema", None)
        if name in POSTGIS_TABLES or schema in POSTGIS_SCHEMAS:
            return False
    return True


def run_migrations_offline() -> None:
    """Emit migration SQL to stdout without connecting (`alembic upgrade --sql`)."""
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_object=include_object,
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = create_async_engine(database_url, poolclass=pool.NullPool)

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
