from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")


@dataclass(frozen=True)
class Settings:
    bot_token: str
    gemini_key: str
    gemini_model: str
    db_path: Path
    log_level: str
    knowledge_dir: Path

    @classmethod
    def load(cls) -> "Settings":
        bot_token = os.getenv("bot_token") or os.getenv("BOT_TOKEN")
        gemini_key = os.getenv("gemini_key") or os.getenv("GEMINI_KEY")
        if not bot_token:
            raise RuntimeError(".env da bot_token aniqlanmagan")
        if not gemini_key:
            raise RuntimeError(".env da gemini_key aniqlanmagan")

        db_path = ROOT_DIR / os.getenv("db_path", "data/bot.db")
        db_path.parent.mkdir(parents=True, exist_ok=True)

        return cls(
            bot_token=bot_token,
            gemini_key=gemini_key,
            gemini_model=os.getenv("gemini_model", "gemini-2.5-flash"),
            db_path=db_path,
            log_level=os.getenv("log_level", "INFO"),
            knowledge_dir=ROOT_DIR / "pos",
        )


settings = Settings.load()
