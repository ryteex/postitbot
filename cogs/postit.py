"""
PostIt cog — all slash commands and the internal scheduler.

Command tree:
    /postit create   — schedule a new message or reminder
    /postit list     — paginated list of active events
    /postit edit     — modify an existing event
    /postit delete   — soft-delete an event
    /postit timezone — set the server's timezone

Scheduler:
    A discord.ext.tasks loop fires every SCHEDULER_INTERVAL seconds.
    It queries the database for events whose next_run <= now, fires them,
    then either deactivates them (one-time) or advances their next_run
    (recurring) — without any external cron daemon.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Optional

import discord
import pytz
from discord import app_commands
from discord.ext import commands, tasks

from config import Config
from db.database import Database
from db.models import Event, GuildSettings
from utils.formatting import build_created_embed, build_list_embed
from utils.permissions import can_manage_events, can_manage_guild_settings, can_modify_event
from utils.recurrence import (
    RecurrenceError,
    compute_first_run,
    compute_next_run,
    describe_recurrence,
    parse_datetime,
    parse_recurrence,
)

logger = logging.getLogger(__name__)


# ── Bulk-create modal ─────────────────────────────────────────────────────────

class BulkCreateModal(discord.ui.Modal, title="Créer plusieurs événements"):
    """
    Modal Discord (popup) permettant de coller plusieurs événements d'un coup.

    Format attendu — une ligne par événement :
        récurrence | contenu
        récurrence | contenu

    Exemples :
        every monday at 11:45 | 👟 Course dans 15 minutes !
        every monday at 14:45 | 🎣 Pêche dans 15 minutes !
        2026-03-10 15:00 | Rappel one-shot

    Les lignes vides ou commençant par # sont ignorées.
    """

    events_input = discord.ui.TextInput(
        label="Événements (un par ligne)",
        placeholder=(
            "every monday at 11:45 | 👟 Course dans 15 minutes !\n"
            "every monday at 14:45 | 🎣 Pêche dans 15 minutes !\n"
            "every monday at 18:15 | 🔨 Enchères dans 15 minutes !"
        ),
        style=discord.TextStyle.paragraph,
        max_length=4000,
        required=True,
    )

    def __init__(
        self,
        cog: "PostItCog",
        channel: discord.TextChannel,
        tz_str: str,
    ) -> None:
        super().__init__()
        self.cog = cog
        self.channel = channel
        self.tz_str = tz_str

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        tz = pytz.timezone(self.tz_str)
        now = datetime.now(tz)
        lines = self.events_input.value.splitlines()

        created: list[str] = []
        errors: list[str] = []

        for i, raw_line in enumerate(lines, start=1):
            line = raw_line.strip()
            # Ignore lignes vides et commentaires
            if not line or line.startswith("#"):
                continue

            # Séparateur attendu : |
            if "|" not in line:
                errors.append(f"Ligne {i} — séparateur `|` manquant : `{line[:60]}`")
                continue

            left, _, content = line.partition("|")
            left = left.strip()
            content = content.strip()

            if not content:
                errors.append(f"Ligne {i} — contenu vide.")
                continue

            if len(content) > 2000:
                errors.append(f"Ligne {i} — contenu trop long (max 2000 car.).")
                continue

            # Déterminer si c'est une récurrence ou une date précise
            recurrence_dict: Optional[dict] = None
            fire_dt: Optional[datetime] = None

            try:
                recurrence_dict = parse_recurrence(left)
                fire_dt = compute_first_run(recurrence_dict, now)
            except RecurrenceError:
                # Ce n'est pas une récurrence — essayer comme date/heure
                try:
                    fire_dt = parse_datetime(left, tz)
                    if fire_dt <= now:
                        errors.append(
                            f"Ligne {i} — date dans le passé : `{left}`"
                        )
                        continue
                except ValueError:
                    errors.append(
                        f"Ligne {i} — format non reconnu : `{left[:60]}`\n"
                        f"  → Attendu : `every monday at 11:45` ou `2026-03-10 15:00`"
                    )
                    continue

            event_id = await Event.create(
                db=self.cog.db,
                guild_id=interaction.guild_id,
                channel_id=self.channel.id,
                creator_id=interaction.user.id,
                event_type="message",
                content=content,
                next_run=int(fire_dt.timestamp()),
                recurrence=recurrence_dict,
            )

            recur_label = (
                describe_recurrence(recurrence_dict) if recurrence_dict
                else fire_dt.strftime("%Y-%m-%d %H:%M %Z")
            )
            created.append(f"✅ `#{event_id}` — {recur_label} — {content[:60]}")

        # ── Réponse récapitulative ────────────────────────────────────────────
        embed = discord.Embed(
            title=f"📌 Import en lot — {len(created)} créé(s), {len(errors)} erreur(s)",
            color=discord.Color.green() if not errors else discord.Color.orange(),
        )
        if created:
            embed.add_field(
                name="Créés",
                value="\n".join(created[:20]) + ("…" if len(created) > 20 else ""),
                inline=False,
            )
        if errors:
            embed.add_field(
                name="Erreurs",
                value="\n".join(errors[:10]),
                inline=False,
            )
        embed.set_footer(text=f"Salon cible : #{self.channel.name}  •  TZ : {self.tz_str}")
        await interaction.followup.send(embed=embed, ephemeral=True)


# ── Pagination view ────────────────────────────────────────────────────────────

class PaginatedListView(discord.ui.View):
    """Buttons-based pagination for /postit list."""

    def __init__(
        self,
        db: Database,
        guild_id: int,
        tz_str: str,
        total: int,
        page_size: int,
    ) -> None:
        super().__init__(timeout=120)   # auto-disables after 2 minutes of inactivity
        self.db = db
        self.guild_id = guild_id
        self.tz_str = tz_str
        self.total = total
        self.page_size = page_size
        self.current_page = 0
        self.max_page = max(0, (total - 1) // page_size)
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        self.btn_prev.disabled = self.current_page == 0
        self.btn_next.disabled = self.current_page >= self.max_page

    async def build_embed(self, page: int) -> discord.Embed:
        events = await Event.list_for_guild(
            self.db, self.guild_id,
            offset=page * self.page_size,
            limit=self.page_size,
        )
        return build_list_embed(
            events=events,
            page=page,
            total_pages=self.max_page + 1,
            total=self.total,
            tz_str=self.tz_str,
        )

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.secondary)
    async def btn_prev(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        self.current_page -= 1
        self._sync_buttons()
        embed = await self.build_embed(self.current_page)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def btn_next(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        self.current_page += 1
        self._sync_buttons()
        embed = await self.build_embed(self.current_page)
        await interaction.response.edit_message(embed=embed, view=self)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]


# ── Cog ────────────────────────────────────────────────────────────────────────

class PostItCog(commands.Cog, name="PostIt"):
    """Scheduled messages and reminders for your server."""

    # ── Command group definition ──────────────────────────────────────────────

    postit = app_commands.Group(
        name="postit",
        description="Manage scheduled messages and reminders",
    )

    # ── Constructor / lifecycle ───────────────────────────────────────────────

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.db: Database = bot.db  # type: ignore[attr-defined]

    async def cog_load(self) -> None:
        """Start the scheduler loop when the cog is loaded."""
        self.scheduler.start()
        logger.info(
            "Scheduler started (interval: %ds).", Config.SCHEDULER_INTERVAL
        )

    def cog_unload(self) -> None:
        """Stop the scheduler loop when the cog is unloaded."""
        self.scheduler.cancel()
        logger.info("Scheduler stopped.")

    # ── Internal scheduler ────────────────────────────────────────────────────

    @tasks.loop(seconds=Config.SCHEDULER_INTERVAL)
    async def scheduler(self) -> None:
        """
        Poll the database every SCHEDULER_INTERVAL seconds.

        Any event whose next_run timestamp is in the past is executed.
        • One-time events are deactivated afterwards.
        • Recurring events have their next_run advanced to the next occurrence.

        Why a polling loop instead of an external cron?
        - No external dependencies to manage or crash.
        - next_run values persist in SQLite across restarts, so no events
          are lost when the bot goes offline.
        - Skipped slots (bot downtime) are handled gracefully: recurring events
          advance their next_run forward without firing multiple times.
        """
        now_ts = int(time.time())
        try:
            due = await Event.get_due(self.db, now_ts)
        except Exception as exc:
            logger.error("Scheduler: failed to query due events: %s", exc)
            return

        for event in due:
            await self._fire_event(event)

    @scheduler.before_loop
    async def _before_scheduler(self) -> None:
        """Wait until the bot is fully ready before the first scheduler tick."""
        await self.bot.wait_until_ready()

    async def _fire_event(self, event: Event) -> None:
        """
        Send the event's content to its channel and update the database.

        If the channel no longer exists or the bot lacks permission, the event
        is logged but NOT deactivated — a temporary outage should not
        permanently remove a legitimate recurring event.
        """
        channel = self.bot.get_channel(event.channel_id)
        if channel is None:
            # Channel might be in a guild that hasn't been cached yet.
            try:
                channel = await self.bot.fetch_channel(event.channel_id)
            except discord.NotFound:
                logger.warning(
                    "Event #%d: channel %d no longer exists — deactivating.",
                    event.id, event.channel_id,
                )
                await Event.deactivate(self.db, event.id)
                return
            except discord.HTTPException as exc:
                logger.error(
                    "Event #%d: could not fetch channel %d: %s",
                    event.id, event.channel_id, exc,
                )
                return

        # Build the message content.
        if event.event_type == "reminder":
            # Reminder: ping the creator.
            content = f"<@{event.creator_id}> 🔔 **Reminder:** {event.content}"
        else:
            content = event.content

        try:
            await channel.send(content)  # type: ignore[union-attr]
            logger.info(
                "Fired event #%d (%s) → channel #%s (guild %d).",
                event.id, event.event_type,
                getattr(channel, "name", channel.id),
                event.guild_id,
            )
        except discord.Forbidden:
            logger.error(
                "Event #%d: missing Send Messages permission in channel %d.",
                event.id, event.channel_id,
            )
            # Don't deactivate — the admin may fix the permissions.
            return
        except discord.HTTPException as exc:
            logger.error("Event #%d: send failed: %s", event.id, exc)
            return

        # Advance or deactivate.
        if event.recurrence:
            tz_str = await GuildSettings.get_timezone(self.db, event.guild_id)
            tz = pytz.timezone(tz_str)
            last_scheduled = datetime.fromtimestamp(event.next_run, tz=tz)
            next_dt = compute_next_run(event.recurrence, last_scheduled)
            await Event.update_next_run(self.db, event.id, int(next_dt.timestamp()))
            logger.debug(
                "Event #%d rescheduled to %s.", event.id, next_dt.isoformat()
            )
        else:
            await Event.deactivate(self.db, event.id)
            logger.debug("Event #%d (one-time) deactivated.", event.id)

    # ── /postit create ────────────────────────────────────────────────────────

    @postit.command(name="create", description="Schedule a new message or reminder")
    @app_commands.describe(
        event_type="'message' posts to the channel; 'reminder' also pings you",
        channel="Text channel where the message will be sent",
        content="Content of the message or reminder (up to 2000 characters)",
        when=(
            "When to send — e.g. '2026-03-10 15:00', 'tomorrow at 9:00', "
            "'14:30'. Optional if recurrence is set."
        ),
        recurrence=(
            "Optional recurrence — e.g. 'every day at 9:00', "
            "'every tuesday at 15:00', 'every 30 minutes', "
            "'every month on the 1st at 9:00'"
        ),
    )
    @app_commands.choices(event_type=[
        app_commands.Choice(name="Message (plain post)", value="message"),
        app_commands.Choice(name="Reminder (pings you)", value="reminder"),
    ])
    async def postit_create(
        self,
        interaction: discord.Interaction,
        event_type: str,
        channel: discord.TextChannel,
        content: str,
        when: Optional[str] = None,
        recurrence: Optional[str] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        # ── Permission check ──────────────────────────────────────────────────
        if not can_manage_events(interaction.user):  # type: ignore[arg-type]
            await interaction.followup.send(
                "You need the **Manage Messages** permission to schedule events.",
                ephemeral=True,
            )
            return

        # ── Content length guard ──────────────────────────────────────────────
        if len(content) > 2000:
            await interaction.followup.send(
                "Content exceeds Discord's 2000-character limit.",
                ephemeral=True,
            )
            return

        # ── Parse recurrence ──────────────────────────────────────────────────
        recurrence_dict: Optional[dict] = None
        if recurrence:
            try:
                recurrence_dict = parse_recurrence(recurrence)
            except RecurrenceError as exc:
                await interaction.followup.send(
                    f"**Invalid recurrence:** {exc}", ephemeral=True
                )
                return

        # ── Determine fire time ───────────────────────────────────────────────
        tz_str = await GuildSettings.get_timezone(self.db, interaction.guild_id)
        tz = pytz.timezone(tz_str)
        now = datetime.now(tz)

        if when:
            try:
                fire_dt = parse_datetime(when, tz)
            except ValueError as exc:
                await interaction.followup.send(
                    f"**Invalid date/time:** {exc}", ephemeral=True
                )
                return
        elif recurrence_dict:
            # No explicit start time: compute from the recurrence rule.
            fire_dt = compute_first_run(recurrence_dict, now)
        else:
            await interaction.followup.send(
                "Provide at least `when` (for a one-time event) "
                "or `recurrence` (for a recurring event), or both.",
                ephemeral=True,
            )
            return

        # Reject past times for one-time events.
        if fire_dt <= now and not recurrence_dict:
            await interaction.followup.send(
                f"The time **{fire_dt.strftime('%Y-%m-%d %H:%M %Z')}** "
                "is already in the past.",
                ephemeral=True,
            )
            return

        # ── Persist ───────────────────────────────────────────────────────────
        event_id = await Event.create(
            db=self.db,
            guild_id=interaction.guild_id,
            channel_id=channel.id,
            creator_id=interaction.user.id,
            event_type=event_type,
            content=content,
            next_run=int(fire_dt.timestamp()),
            recurrence=recurrence_dict,
        )

        embed = build_created_embed(
            event_id=event_id,
            event_type=event_type,
            channel=channel,
            content=content,
            fire_dt=fire_dt,
            recurrence=recurrence_dict,
            tz_str=tz_str,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /postit list ──────────────────────────────────────────────────────────

    @postit.command(
        name="list",
        description="List all active scheduled events on this server",
    )
    async def postit_list(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        total = await Event.count_for_guild(self.db, interaction.guild_id)
        if total == 0:
            await interaction.followup.send(
                "No active scheduled events on this server.\n"
                "Use `/postit create` to add one!",
                ephemeral=True,
            )
            return

        tz_str = await GuildSettings.get_timezone(self.db, interaction.guild_id)
        view = PaginatedListView(
            db=self.db,
            guild_id=interaction.guild_id,
            tz_str=tz_str,
            total=total,
            page_size=Config.PAGE_SIZE,
        )
        embed = await view.build_embed(page=0)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    # ── /postit delete ────────────────────────────────────────────────────────

    @postit.command(name="delete", description="Delete a scheduled event by its ID")
    @app_commands.describe(event_id="Event ID shown in /postit list")
    async def postit_delete(
        self, interaction: discord.Interaction, event_id: int
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        event = await Event.get_by_id(self.db, event_id)

        # Validate existence and guild ownership.
        if not event or not event.is_active or event.guild_id != interaction.guild_id:
            await interaction.followup.send(
                f"Event **#{event_id}** not found.", ephemeral=True
            )
            return

        # Permission: creator or server manager.
        if not can_modify_event(interaction.user, event.creator_id):  # type: ignore[arg-type]
            await interaction.followup.send(
                "You can only delete your own events, "
                "unless you have **Manage Server** permission.",
                ephemeral=True,
            )
            return

        await Event.deactivate(self.db, event_id)
        await interaction.followup.send(
            f"Event **#{event_id}** has been deleted.", ephemeral=True
        )

    # ── /postit edit ──────────────────────────────────────────────────────────

    @postit.command(
        name="edit",
        description="Edit the content, channel, time, or recurrence of an event",
    )
    @app_commands.describe(
        event_id="Event ID shown in /postit list",
        content="New content (leave empty to keep current)",
        channel="New target channel (leave empty to keep current)",
        when="New scheduled time (leave empty to keep current)",
        recurrence=(
            "New recurrence rule, or 'none' to make the event one-time "
            "(leave empty to keep current)"
        ),
    )
    async def postit_edit(
        self,
        interaction: discord.Interaction,
        event_id: int,
        content: Optional[str] = None,
        channel: Optional[discord.TextChannel] = None,
        when: Optional[str] = None,
        recurrence: Optional[str] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        # ── Fetch and validate ────────────────────────────────────────────────
        event = await Event.get_by_id(self.db, event_id)
        if not event or not event.is_active or event.guild_id != interaction.guild_id:
            await interaction.followup.send(
                f"Event **#{event_id}** not found.", ephemeral=True
            )
            return

        if not can_modify_event(interaction.user, event.creator_id):  # type: ignore[arg-type]
            await interaction.followup.send(
                "You can only edit your own events, "
                "unless you have **Manage Server** permission.",
                ephemeral=True,
            )
            return

        if not any([content, channel, when, recurrence]):
            await interaction.followup.send(
                "Provide at least one field to update.", ephemeral=True
            )
            return

        # ── Build update kwargs ───────────────────────────────────────────────
        kwargs: dict = {}

        if content:
            if len(content) > 2000:
                await interaction.followup.send(
                    "Content exceeds Discord's 2000-character limit.", ephemeral=True
                )
                return
            kwargs["content"] = content

        if channel:
            kwargs["channel_id"] = channel.id

        if recurrence:
            if recurrence.strip().lower() == "none":
                kwargs["clear_recurrence"] = True
            else:
                try:
                    kwargs["recurrence"] = parse_recurrence(recurrence)
                except RecurrenceError as exc:
                    await interaction.followup.send(
                        f"**Invalid recurrence:** {exc}", ephemeral=True
                    )
                    return

        if when:
            tz_str = await GuildSettings.get_timezone(self.db, interaction.guild_id)
            tz = pytz.timezone(tz_str)
            try:
                fire_dt = parse_datetime(when, tz)
                kwargs["next_run"] = int(fire_dt.timestamp())
            except ValueError as exc:
                await interaction.followup.send(
                    f"**Invalid date/time:** {exc}", ephemeral=True
                )
                return

        await Event.edit(self.db, event_id, **kwargs)
        await interaction.followup.send(
            f"Event **#{event_id}** updated successfully.", ephemeral=True
        )

    # ── /postit timezone ──────────────────────────────────────────────────────

    @postit.command(
        name="timezone",
        description="Set the timezone used for all scheduled times on this server",
    )
    @app_commands.describe(
        timezone=(
            "IANA timezone name — e.g. 'Europe/Paris', "
            "'America/New_York', 'Asia/Tokyo', 'UTC'"
        )
    )
    async def postit_timezone(
        self, interaction: discord.Interaction, timezone: str
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        if not can_manage_guild_settings(interaction.user):  # type: ignore[arg-type]
            await interaction.followup.send(
                "You need the **Manage Server** permission to change the timezone.",
                ephemeral=True,
            )
            return

        try:
            tz = pytz.timezone(timezone)
            canonical = tz.zone  # normalise to pytz canonical name
        except pytz.exceptions.UnknownTimeZoneError:
            await interaction.followup.send(
                f"`{timezone}` is not a recognised timezone.\n"
                "Use a standard IANA name such as `Europe/Paris`, "
                "`America/New_York`, `Asia/Tokyo`, or `UTC`.\n"
                "Full list: <https://en.wikipedia.org/wiki/List_of_tz_database_time_zones>",
                ephemeral=True,
            )
            return

        await GuildSettings.set_timezone(self.db, interaction.guild_id, canonical)

        now = datetime.now(tz)
        await interaction.followup.send(
            f"Server timezone set to **{canonical}**.\n"
            f"Current local time: **{now.strftime('%Y-%m-%d %H:%M')}**",
            ephemeral=True,
        )

    # ── /postit bulk ──────────────────────────────────────────────────────────

    @postit.command(
        name="bulk",
        description="Créer plusieurs événements d'un coup via un formulaire",
    )
    @app_commands.describe(channel="Salon cible pour tous les événements")
    async def postit_bulk(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        if not can_manage_events(interaction.user):  # type: ignore[arg-type]
            await interaction.response.send_message(
                "Tu as besoin de la permission **Gérer les messages** pour planifier des événements.",
                ephemeral=True,
            )
            return

        tz_str = await GuildSettings.get_timezone(self.db, interaction.guild_id)
        modal = BulkCreateModal(cog=self, channel=channel, tz_str=tz_str)
        await interaction.response.send_modal(modal)

    # ── Global error handler ──────────────────────────────────────────────────

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        """Catch any unhandled app command errors and report them gracefully."""
        # Unwrap CommandInvokeError pour accéder à l'exception originale
        original = getattr(error, "original", error)

        msg = "An unexpected error occurred. Please try again later."

        if isinstance(error, app_commands.CommandOnCooldown):
            msg = f"Command on cooldown. Try again in {error.retry_after:.1f}s."
        elif isinstance(error, app_commands.MissingPermissions):
            msg = "You don't have the required permissions for this command."
        elif isinstance(error, app_commands.BotMissingPermissions):
            msg = (
                "I'm missing permissions needed to execute this command. "
                "Please check my role settings."
            )

        logger.error(
            "App command error in '%s': %s",
            interaction.command.name if interaction.command else "unknown",
            original,
            exc_info=original,
        )

        # Respond only if we haven't already sent a response.
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass


# ── Extension entrypoint ──────────────────────────────────────────────────────

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PostItCog(bot))
