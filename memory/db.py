"""
Async PostgreSQL client for the AI Factory memory layer.

Usage:
    db = MemoryDB(dsn="postgresql://user:pass@host/dbname")
    await db.connect()
    rows = await db.fetch("SELECT * FROM episodes WHERE id = $1", episode_id)
    await db.close()
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any

import asyncpg

LOGGER = logging.getLogger(__name__)

_DEFAULT_DSN = os.environ.get(
    "MEMORY_DB_URL",
    "postgresql://temporal:temporal@localhost:5432/ai_factory_memory",
)


class MemoryDB:
    def __init__(self, dsn: str = _DEFAULT_DSN) -> None:
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        self._pool = await asyncpg.create_pool(
            self._dsn,
            min_size=2,
            max_size=10,
            command_timeout=30,
        )
        LOGGER.info("MemoryDB pool connected")

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            self._pool = None
            LOGGER.info("MemoryDB pool closed")

    @asynccontextmanager
    async def transaction(self):
        """Yield a connection inside a transaction block."""
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                yield conn

    async def execute(self, query: str, *args: Any) -> str:
        async with self._pool.acquire() as conn:
            return await conn.execute(query, *args)

    async def fetch(self, query: str, *args: Any) -> list[dict]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, *args)
            return [dict(r) for r in rows]

    async def fetchrow(self, query: str, *args: Any) -> dict | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(query, *args)
            return dict(row) if row else None

    async def fetchval(self, query: str, *args: Any) -> Any:
        async with self._pool.acquire() as conn:
            return await conn.fetchval(query, *args)
