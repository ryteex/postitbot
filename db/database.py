"""
Database connection and schema initialisation.

A single Database instance is created at bot startup and shared across all cogs.
"""

import logging

import aiosqlite

logger = logging.getLogger(__name__)

# ── SQL schema ────────────────────────────────────────────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id  INTEGER PRIMARY KEY,
    timezone  TEXT    NOT NULL DEFAULT 'UTC'
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,
    creator_id  INTEGER NOT NULL,
    -- 'message' sends plain text; 'reminder' also mentions the creator
    event_type  TEXT    NOT NULL CHECK(event_type IN ('message', 'reminder')),
    content     TEXT    NOT NULL,
    -- Unix timestamp of the next scheduled execution
    next_run    INTEGER NOT NULL,
    -- JSON-encoded recurrence rule, NULL for one-time events
    recurrence  TEXT,
    created_at  INTEGER NOT NULL,
    is_active   INTEGER NOT NULL DEFAULT 1
);

-- Speed up the hot path: "give me all active events due by time T"
CREATE INDEX IF NOT EXISTS idx_events_due
    ON events (next_run, is_active);

-- Speed up listing per guild
CREATE INDEX IF NOT EXISTS idx_events_guild
    ON events (guild_id, is_active);
"""


class Database:
    """Async wrapper around an aiosqlite connection."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def initialize(self) -> None:
        """Open the connection and create tables if they don't exist."""
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        # Allow access to columns by name (e.g. row["guild_id"])
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()
        logger.info("Database ready at %s", self.path)

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None
            logger.info("Database connection closed.")

    # ── Accessor ──────────────────────────────────────────────────────────────

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database.initialize() has not been called yet.")
        return self._conn
