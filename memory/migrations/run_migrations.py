"""
Apply all SQL migrations in order.

Usage:
    python -m memory.migrations
    python memory/migrations/run_migrations.py
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import asyncpg

LOGGER = logging.getLogger(__name__)
MIGRATIONS_DIR = Path(__file__).parent


async def run(dsn: str) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        sql_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
        for sql_file in sql_files:
            LOGGER.info(f"Applying migration: {sql_file.name}")
            sql = sql_file.read_text()
            await conn.execute(sql)
            LOGGER.info(f"  ✓ {sql_file.name}")
    finally:
        await conn.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    dsn = os.environ.get(
        "MEMORY_DB_URL",
        "postgresql://temporal:temporal@localhost:5432/ai_factory_memory",
    )
    asyncio.run(run(dsn))


if __name__ == "__main__":
    main()
