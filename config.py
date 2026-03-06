import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    TOKEN: str = os.getenv("DISCORD_TOKEN", "")
    DATABASE_URL: str = os.getenv("DATABASE_URL", "")
    SCHEDULER_INTERVAL: int = int(os.getenv("SCHEDULER_INTERVAL", "30"))
    DEFAULT_TIMEZONE: str = os.getenv("DEFAULT_TIMEZONE", "UTC")
    PAGE_SIZE: int = int(os.getenv("PAGE_SIZE", "5"))
    # ID du serveur de dev pour une sync instantanée (optionnel)
    DEV_GUILD_ID: int | None = int(os.getenv("DEV_GUILD_ID")) if os.getenv("DEV_GUILD_ID") else None

    @classmethod
    def validate(cls) -> None:
        if not cls.TOKEN:
            raise ValueError(
                "DISCORD_TOKEN is missing. "
                "Create a .env file based on .env.example and set your token."
            )
        if not cls.DATABASE_URL:
            raise ValueError(
                "DATABASE_URL is missing. "
                "Set it to your PostgreSQL connection URL."
            )
