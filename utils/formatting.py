"""
Discord embed / message formatting helpers.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

import discord
import pytz

from utils.recurrence import describe_recurrence


# ── Event-creation confirmation embed ────────────────────────────────────────

def build_created_embed(
    event_id: int,
    event_type: str,
    channel: discord.TextChannel,
    content: str,
    fire_dt: datetime,
    recurrence: Optional[dict],
    tz_str: str,
) -> discord.Embed:
    icon = "📨" if event_type == "message" else "🔔"
    color = discord.Color.green()

    embed = discord.Embed(
        title=f"{icon} Event scheduled — #{event_id}",
        color=color,
    )
    embed.add_field(name="Type", value=event_type.capitalize(), inline=True)
    embed.add_field(name="Channel", value=channel.mention, inline=True)
    embed.add_field(
        name="First run",
        value=fire_dt.strftime("%Y-%m-%d %H:%M %Z"),
        inline=True,
    )
    embed.add_field(
        name="Recurrence",
        value=describe_recurrence(recurrence) if recurrence else "One-time",
        inline=True,
    )
    embed.add_field(
        name="Content",
        value=content[:1000],
        inline=False,
    )
    embed.set_footer(text=f"Server timezone: {tz_str}  •  Use /postit list to view all events.")
    return embed


# ── Paginated event list ──────────────────────────────────────────────────────

def build_list_embed(
    events: list,
    page: int,
    total_pages: int,
    total: int,
    tz_str: str,
) -> discord.Embed:
    tz = pytz.timezone(tz_str)
    embed = discord.Embed(
        title="📌 Scheduled Events",
        color=discord.Color.blue(),
    )
    embed.set_footer(
        text=(
            f"Page {page + 1}/{total_pages}  •  "
            f"{total} event{'s' if total != 1 else ''} active  •  "
            f"TZ: {tz_str}"
        )
    )

    for event in events:
        icon = "📨" if event.event_type == "message" else "🔔"
        fire_time = datetime.fromtimestamp(event.next_run, tz=tz)
        recur_text = (
            describe_recurrence(event.recurrence) if event.recurrence else "One-time"
        )
        preview = event.content[:120] + ("…" if len(event.content) > 120 else "")

        field_name = f"{icon} {event.event_type.capitalize()} — {recur_text}"
        field_value = (
            f"🆔 **ID : `{event.id}`** — pour `/postit delete` ou `/postit edit`\n"
            f"**Salon :** <#{event.channel_id}>\n"
            f"**Prochain envoi :** {fire_time.strftime('%Y-%m-%d %H:%M %Z')}\n"
            f"**Contenu :** {preview}"
        )
        embed.add_field(name=field_name, value=field_value, inline=False)

    return embed
