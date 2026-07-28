"""
asyncpg connection pool.

A single process-wide pool, opened on startup and closed on shutdown. Handlers
acquire a connection per request via the `db` dependency; transactions are
opened explicitly where a unit of work spans several statements (e.g. a
soft-delete: export to S3, then delete rows, then write the manifest).
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg

from app.core.config import settings

_pool: asyncpg.Pool | None = None


async def open_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn=settings.database_url,
            min_size=settings.db_pool_min,
            max_size=settings.db_pool_max,
            # Keep numeric() as Decimal; we coerce to float at the edge only.
            command_timeout=30,
        )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool is not open")
    return _pool


@asynccontextmanager
async def connection() -> AsyncIterator[asyncpg.Connection]:
    pool = get_pool()
    async with pool.acquire() as conn:
        yield conn


# FastAPI dependency
async def db() -> AsyncIterator[asyncpg.Connection]:
    async with connection() as conn:
        yield conn
