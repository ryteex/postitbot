"""
Database connection and schema initialisation.

A single Database instance is created at bot startup and shared across all cogs.
"""

import logging

import asyncpg

from config import Config

logger = logging.getLogger(__name__)

# ── SQL schema ────────────────────────────────────────────────────────────────

_SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS guild_settings (
        guild_id  BIGINT PRIMARY KEY,
        timezone  TEXT   NOT NULL DEFAULT 'UTC'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS events (
        id          SERIAL  PRIMARY KEY,
        guild_id    BIGINT  NOT NULL,
        channel_id  BIGINT  NOT NULL,
        creator_id  BIGINT  NOT NULL,
        event_type  TEXT    NOT NULL CHECK(event_type IN ('message', 'reminder')),
        content     TEXT    NOT NULL,
        next_run    BIGINT  NOT NULL,
        recurrence  TEXT,
        created_at  BIGINT  NOT NULL,
        is_active   SMALLINT NOT NULL DEFAULT 1
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_events_due   ON events (next_run, is_active)",
    "CREATE INDEX IF NOT EXISTS idx_events_guild ON events (guild_id, is_active)",
]


class Database:
    """Async wrapper around an asyncpg connection pool."""

    def __init__(self) -> None:
        self._pool: asyncpg.Pool | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def initialize(self) -> None:
        """Open the connection pool and create tables if they don't exist."""
        self._pool = await asyncpg.create_pool(dsn=Config.DATABASE_URL)
        async with self._pool.acquire() as conn:
            for stmt in _SCHEMA_STATEMENTS:
                await conn.execute(stmt)
        logger.info("Database ready (PostgreSQL).")

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            self._pool = None
            logger.info("Database pool closed.")

    # ── Accessor ──────────────────────────────────────────────────────────────

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("Database.initialize() has not been called yet.")
        return self._pool
