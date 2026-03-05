"""
Post-it Discord Bot — entry point.

Loads configuration, initialises the bot, and starts the async event loop.
"""

import asyncio
import logging

from config import Config
from bot.client import PostItBot

# ── Logging setup ────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


async def main() -> None:
    Config.validate()

    bot = PostItBot()
    async with bot:
        await bot.start(Config.TOKEN)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down Post-it bot.")
