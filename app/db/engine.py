"""Engine and pool lifecycle.

Persistence is required (docs/persistence-schema.md §8.1): an unset DATABASE_URL
is a startup failure with a message naming the fix, rather than a quiet fallback
to in-memory state that looks like it worked until someone looks for their data.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from ..config import settings

log = logging.getLogger("report-agent.db")

_HINT = (
    "DATABASE_URL is not set and persistence is required.\n"
    "  local:   docker compose up -d postgres   (or use your own Postgres)\n"
    "  then:    export DATABASE_URL=postgresql+asyncpg://report_agent:report_agent"
    "@localhost:5432/report_agent\n"
    "  migrate: alembic upgrade head"
)


def require_database_url() -> str:
    if not settings.database_url:
        raise RuntimeError(_HINT)
    return settings.database_url


class Database:
    """A lazily-created engine, opened by the app lifespan and closed with it."""

    def __init__(self) -> None:
        self._engine: AsyncEngine | None = None

    async def connect(self) -> None:
        url = require_database_url()
        self._engine = create_async_engine(
            url, pool_size=settings.db_pool_size, max_overflow=2,
            pool_pre_ping=True, echo=settings.db_echo,
        )
        async with self._engine.connect() as conn:      # fail fast, not on first write
            await conn.execute(text("SELECT 1"))
        log.info("database connected (pool_size=%s)", settings.db_pool_size)

    async def close(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None

    @property
    def engine(self) -> AsyncEngine:
        if self._engine is None:
            raise RuntimeError("database is not connected; app lifespan did not run")
        return self._engine

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[AsyncConnection]:
        """A transaction. Commits on success, rolls back on error."""
        async with self.engine.begin() as conn:
            yield conn

    async def healthy(self) -> bool:
        try:
            async with self.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception as exc:
            log.warning("database health check failed: %s", exc)
            return False


db = Database()
