import os
import asyncpg
from pathlib import Path
from typing import Optional
from app.config import settings

_pool: Optional[asyncpg.Pool] = None


async def get_db_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        raise RuntimeError("Database connection pool is not initialized. Call init_db_pool() first.")
    return _pool


async def init_db_pool() -> asyncpg.Pool:
    global _pool
    if _pool is not None:
        return _pool

    if settings.database_url:
        _pool = await asyncpg.create_pool(
            dsn=settings.database_url,
            min_size=settings.db_min_connections,
            max_size=settings.db_max_connections,
        )
    elif settings.postgres_socket_dir:
        _pool = await asyncpg.create_pool(
            host=settings.postgres_socket_dir,
            port=settings.postgres_port,
            user=settings.postgres_user,
            password=settings.postgres_password,
            database=settings.postgres_db,
            min_size=settings.db_min_connections,
            max_size=settings.db_max_connections,
        )
    else:
        _pool = await asyncpg.create_pool(
            host=settings.postgres_host,
            port=settings.postgres_port,
            user=settings.postgres_user,
            password=settings.postgres_password,
            database=settings.postgres_db,
            min_size=settings.db_min_connections,
            max_size=settings.db_max_connections,
        )

    # Initialize schema
    schema_path = Path(__file__).parent / "schema.sql"
    if schema_path.exists():
        schema_sql = schema_path.read_text(encoding="utf-8")
        async with _pool.acquire() as conn:
            await conn.execute(schema_sql)

    return _pool


async def close_db_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
