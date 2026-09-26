"""Alembic environment (async, asyncpg). URL comes from deskmind.config."""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection

from deskmind.config import get_settings
from deskmind.db.models import Base
from deskmind.db.session import create_engine

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# TimescaleDB creates internal objects; keep autogenerate focused on our tables.
TIMESCALE_INTERNAL_SCHEMAS = {
    "_timescaledb_internal",
    "_timescaledb_catalog",
    "timescaledb_information",
}


def include_object(obj, name, type_, reflected, compare_to):
    schema = getattr(obj, "schema", None)
    return schema not in TIMESCALE_INTERNAL_SCHEMAS


def run_migrations_offline() -> None:
    context.configure(
        url=get_settings().database_url,
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
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_engine(get_settings())
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
