from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool, text
from sqlalchemy.ext.asyncio import async_engine_from_config, create_async_engine

from config.settings import get_settings
from database.base import Base
from database.models import (
    ConversationMessageModel,
    ConversationModel,
    ConversationRunModel,
)
from database.runtime import build_database_url


del ConversationMessageModel, ConversationModel, ConversationRunModel
config = context.config
if (
    config.config_file_name is not None
    and config.attributes.get("configure_logger", True)
):
    fileConfig(config.config_file_name, disable_existing_loggers=False)
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.attributes.get("connection_url") or build_database_url(
        get_settings().persistence
    )
    context.configure(
        url=url.render_as_string(hide_password=False),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    # Alembic opens a separate transaction for every revision. MySQL DML can
    # therefore roll back normally. MySQL DDL implicitly commits, so revisions
    # that change a table consolidate their changes into one atomic DDL statement.
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        transaction_per_migration=True,
        transactional_ddl=False,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    supplied_url = config.attributes.get("connection_url")
    if supplied_url is not None:
        connectable = create_async_engine(supplied_url, poolclass=pool.NullPool)
    else:
        section = config.get_section(config.config_ini_section) or {}
        section["sqlalchemy.url"] = build_database_url(
            get_settings().persistence
        ).render_as_string(hide_password=False)
        connectable = async_engine_from_config(
            section,
            prefix="sqlalchemy.",
            poolclass=pool.NullPool,
        )

    lock_name = "new_dh_agent_schema_migration"
    timeout = get_settings().persistence.migration_lock_timeout
    async with connectable.connect() as connection:
        acquired = (
            await connection.execute(
                text("SELECT GET_LOCK(:name, :timeout)"),
                {"name": lock_name, "timeout": timeout},
            )
        ).scalar_one()
        if acquired != 1:
            raise RuntimeError("could not acquire MySQL schema migration lock")
        try:
            await connection.run_sync(do_run_migrations)
        finally:
            await connection.execute(
                text("SELECT RELEASE_LOCK(:name)"), {"name": lock_name}
            )
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
