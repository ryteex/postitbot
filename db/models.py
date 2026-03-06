"""
Data-access layer — all database reads and writes go through these classes.

Keeping SQL out of the cog keeps the command code clean and makes the
database schema easy to change in one place.
"""

from __future__ import annotations

import json
import time
from typing import Optional

from db.database import Database


# ── Guild settings ─────────────────────────────────────────────────────────────

class GuildSettings:
    """Per-guild configuration (currently just the timezone)."""

    @staticmethod
    async def get_timezone(db: Database, guild_id: int) -> str:
        row = await db.pool.fetchrow(
            "SELECT timezone FROM guild_settings WHERE guild_id = $1",
            guild_id,
        )
        return row["timezone"] if row else "UTC"

    @staticmethod
    async def set_timezone(db: Database, guild_id: int, timezone: str) -> None:
        await db.pool.execute(
            "INSERT INTO guild_settings (guild_id, timezone) VALUES ($1, $2) "
            "ON CONFLICT (guild_id) DO UPDATE SET timezone = EXCLUDED.timezone",
            guild_id, timezone,
        )


# ── Event model ────────────────────────────────────────────────────────────────

class Event:
    """A scheduled message or reminder."""

    __slots__ = (
        "id", "guild_id", "channel_id", "creator_id",
        "event_type", "content", "next_run",
        "recurrence", "created_at", "is_active",
    )

    def __init__(self, row) -> None:
        self.id: int = row["id"]
        self.guild_id: int = row["guild_id"]
        self.channel_id: int = row["channel_id"]
        self.creator_id: int = row["creator_id"]
        self.event_type: str = row["event_type"]
        self.content: str = row["content"]
        self.next_run: int = row["next_run"]
        # Deserialise the recurrence JSON once, store as dict or None
        self.recurrence: Optional[dict] = (
            json.loads(row["recurrence"]) if row["recurrence"] else None
        )
        self.created_at: int = row["created_at"]
        self.is_active: bool = bool(row["is_active"])

    # ── Write operations ──────────────────────────────────────────────────────

    @staticmethod
    async def create(
        db: Database,
        guild_id: int,
        channel_id: int,
        creator_id: int,
        event_type: str,
        content: str,
        next_run: int,
        recurrence: Optional[dict] = None,
    ) -> int:
        """Insert a new event and return its auto-incremented ID."""
        recurrence_json = json.dumps(recurrence) if recurrence else None
        row = await db.pool.fetchrow(
            "INSERT INTO events "
            "    (guild_id, channel_id, creator_id, event_type, "
            "     content, next_run, recurrence, created_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8) "
            "RETURNING id",
            guild_id, channel_id, creator_id, event_type,
            content, next_run, recurrence_json, int(time.time()),
        )
        return row["id"]

    @staticmethod
    async def update_next_run(db: Database, event_id: int, next_run: int) -> None:
        """Move a recurring event to its next scheduled time."""
        await db.pool.execute(
            "UPDATE events SET next_run = $1 WHERE id = $2",
            next_run, event_id,
        )

    @staticmethod
    async def deactivate(db: Database, event_id: int) -> None:
        """Soft-delete an event (preserves history)."""
        await db.pool.execute(
            "UPDATE events SET is_active = 0 WHERE id = $1",
            event_id,
        )

    @staticmethod
    async def edit(
        db: Database,
        event_id: int,
        content: Optional[str] = None,
        channel_id: Optional[int] = None,
        next_run: Optional[int] = None,
        recurrence: Optional[dict] = None,
        clear_recurrence: bool = False,
    ) -> None:
        """
        Partially update an event.

        Pass `clear_recurrence=True` to turn a recurring event into a one-shot.
        Setting `recurrence` to a dict AND `clear_recurrence=True` is not
        meaningful; `clear_recurrence` wins for the recurrence column.
        """
        updates: list[str] = []
        params: list = []
        idx = 1

        if content is not None:
            updates.append(f"content = ${idx}")
            params.append(content)
            idx += 1
        if channel_id is not None:
            updates.append(f"channel_id = ${idx}")
            params.append(channel_id)
            idx += 1
        if next_run is not None:
            updates.append(f"next_run = ${idx}")
            params.append(next_run)
            idx += 1
        if clear_recurrence:
            updates.append("recurrence = NULL")
        elif recurrence is not None:
            updates.append(f"recurrence = ${idx}")
            params.append(json.dumps(recurrence))
            idx += 1

        if not updates:
            return

        params.append(event_id)
        await db.pool.execute(
            f"UPDATE events SET {', '.join(updates)} WHERE id = ${idx}",
            *params,
        )

    # ── Read operations ───────────────────────────────────────────────────────

    @staticmethod
    async def get_by_id(db: Database, event_id: int) -> Optional["Event"]:
        row = await db.pool.fetchrow(
            "SELECT * FROM events WHERE id = $1", event_id
        )
        return Event(row) if row else None

    @staticmethod
    async def get_due(db: Database, until: int) -> list["Event"]:
        """Return all active events whose next_run is <= `until`."""
        rows = await db.pool.fetch(
            "SELECT * FROM events WHERE next_run <= $1 AND is_active = 1 "
            "ORDER BY next_run ASC",
            until,
        )
        return [Event(r) for r in rows]

    @staticmethod
    async def list_for_guild(
        db: Database, guild_id: int, offset: int = 0, limit: int = 5
    ) -> list["Event"]:
        rows = await db.pool.fetch(
            "SELECT * FROM events WHERE guild_id = $1 AND is_active = 1 "
            "ORDER BY next_run ASC LIMIT $2 OFFSET $3",
            guild_id, limit, offset,
        )
        return [Event(r) for r in rows]

    @staticmethod
    async def count_for_guild(db: Database, guild_id: int) -> int:
        row = await db.pool.fetchrow(
            "SELECT COUNT(*) FROM events WHERE guild_id = $1 AND is_active = 1",
            guild_id,
        )
        return row[0]
