"""
PostItBot — custom Bot subclass.

Responsibilities:
- Initialise the database on startup.
- Load the PostIt cog (which registers all slash commands).
- Sync the command tree globally so slash commands appear in Discord.
- Cleanly close the database on shutdown.
"""

from __future__ import annotations

import logging

import discord
from discord.ext import commands

from config import Config
from db.database import Database

logger = logging.getLogger(__name__)


class PostItBot(commands.Bot):

    def __init__(self) -> None:
        intents = discord.Intents.default()
        # We don't need privileged intents (no message content needed).
        super().__init__(
            command_prefix="!",      # Unused — all commands are slash commands.
            intents=intents,
            help_command=None,
        )
        self.db = Database()

    # ── Startup ───────────────────────────────────────────────────────────────

    async def setup_hook(self) -> None:
        """Called once by discord.py before the bot logs in.

        This is the correct place to perform async initialisation (DB, cogs)
        and to sync the command tree for the first time.
        """
        await self.db.initialize()
        await self.load_extension("cogs.postit")

        if Config.DEV_GUILD_ID:
            # Sync instantané sur le serveur de dev (< 5 secondes)
            guild = discord.Object(id=Config.DEV_GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            logger.info("Commandes syncées sur le serveur de dev (ID: %d).", Config.DEV_GUILD_ID)
        else:
            # Sync globale — peut prendre jusqu'à 1 heure sur Discord
            await self.tree.sync()
            logger.info("Application commands synced globally.")

    async def on_ready(self) -> None:
        logger.info(
            "Post-it bot ready — logged in as %s (ID: %s), "
            "serving %d guild(s).",
            self.user,
            self.user.id,  # type: ignore[union-attr]
            len(self.guilds),
        )
        activity = discord.Activity(
            type=discord.ActivityType.watching,
            name="/postit — scheduling made easy",
        )
        await self.change_presence(activity=activity)

    async def on_guild_join(self, guild: discord.Guild) -> None:
        logger.info("Joined guild: %s (ID: %d)", guild.name, guild.id)

    # ── Shutdown ──────────────────────────────────────────────────────────────

    async def close(self) -> None:
        await self.db.close()
        await super().close()
