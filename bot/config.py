from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True, slots=True)
class Config:
    bot_token: str
    admin_ids: frozenset[int]
    database_path: str

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv()

        token = os.getenv("BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError("BOT_TOKEN is missing. Add it to Railway Variables or your .env file.")

        raw_admin_ids = os.getenv("ADMIN_IDS", "").strip()
        if not raw_admin_ids:
            raise RuntimeError("ADMIN_IDS is missing. Add your numeric Telegram ID.")

        try:
            admin_ids = frozenset(
                int(item.strip()) for item in raw_admin_ids.split(",") if item.strip()
            )
        except ValueError as exc:
            raise RuntimeError("ADMIN_IDS must contain only numeric Telegram IDs.") from exc

        if not admin_ids:
            raise RuntimeError("At least one administrator ID is required.")

        # On Railway, mount a persistent Volume to /data. Locally, the project data/ folder is used.
        default_path = "/data/rewards_bot.db" if Path("/data").exists() else "data/rewards_bot.db"
        database_path = os.getenv("DATABASE_PATH", default_path).strip()

        return cls(bot_token=token, admin_ids=admin_ids, database_path=database_path)

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.admin_ids
