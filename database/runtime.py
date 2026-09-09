from __future__ import annotations

import asyncio
import logging
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from config.settings import PersistenceSettings, get_settings


logger = logging.getLogger(__name__)


def build_database_url(settings: PersistenceSettings) -> URL:
    return URL.create(
        "mysql+asyncmy",
        username=settings.user,
        password=settings.password.get_secret_value(),
        host=settings.host,
        port=settings.port,
        database=settings.name,
        query={"charset": "utf8mb4"},
    )


def build_server_url(settings: PersistenceSettings) -> URL:
    return URL.create(
        "mysql+asyncmy",
        username=settings.user,
        password=settings.password.get_secret_value(),
        host=settings.host,
        port=settings.port,
        query={"charset": "utf8mb4"},
    )


class DatabaseRuntime:
    def __init__(self) -> None:
        self._engine: Optional[AsyncEngine] = None
        self._session_factory: Optional[async_sessionmaker[AsyncSession]] = None

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        if self._session_factory is None:
            raise RuntimeError("database runtime has not been started")
        return self._session_factory

    async def start(self) -> None:
        settings = get_settings().persistence
        if self._engine is not None:
            return
        await self._ensure_database(settings)
        if settings.migration_on_startup:
            from database.migration import upgrade_to_head

            await asyncio.to_thread(upgrade_to_head)
        engine = create_async_engine(
            build_database_url(settings),
            pool_pre_ping=True,
            pool_size=settings.pool_size,
            max_overflow=settings.max_overflow,
            pool_recycle=settings.pool_recycle_seconds,
            connect_args={"connect_timeout": settings.connect_timeout},
        )
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except Exception:
            await engine.dispose()
            raise
        self._engine = engine
        self._session_factory = async_sessionmaker(
            engine, expire_on_commit=False, autoflush=False
        )
        logger.info(
            "application_persistence_started host=%s port=%s database=%s",
            settings.host,
            settings.port,
            settings.name,
        )

    async def _ensure_database(self, settings: PersistenceSettings) -> None:
        engine = create_async_engine(
            build_server_url(settings),
            poolclass=NullPool,
            connect_args={"connect_timeout": settings.connect_timeout},
        )
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        f"CREATE DATABASE IF NOT EXISTS `{settings.name}` "
                        "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                    )
                )
        finally:
            await engine.dispose()

    async def ping(self) -> bool:
        if self._engine is None:
            return False
        try:
            async with self._engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
            return True
        except Exception:
            logger.exception("application_persistence_ping_failed")
            return False

    async def close(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
        self._engine = None
        self._session_factory = None


database_runtime = DatabaseRuntime()
