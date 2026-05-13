from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")


def _get(*keys: str) -> str | None:
    for key in keys:
        v = os.getenv(key)
        if v is not None and v != "":
            return v
    return None


@dataclass(frozen=True)
class Settings:
    bot_token: str
    gemini_key: str
    gemini_model: str
    db_path: Path
    log_level: str
    knowledge_dir: Path
    enable_gemini_cache: bool
    operator_chat_id: int | None
    throttle_seconds: float
    elevenlabs_key: str | None
    elevenlabs_voice_id: str
    voice_probability: float

    @classmethod
    def load(cls) -> "Settings":
        bot_token = _get("bot_token", "BOT_TOKEN")
        gemini_key = _get("gemini_key", "GEMINI_KEY")
        if not bot_token:
            raise RuntimeError(".env da bot_token aniqlanmagan")
        if not gemini_key:
            raise RuntimeError(".env da gemini_key aniqlanmagan")

        db_path = ROOT_DIR / (_get("db_path", "DB_PATH") or "data/bot.db")
        db_path.parent.mkdir(parents=True, exist_ok=True)

        operator_chat = _get("operator_chat_id", "OPERATOR_CHAT_ID")
        operator_chat_id = int(operator_chat) if operator_chat else None

        return cls(
            bot_token=bot_token,
            gemini_key=gemini_key,
            gemini_model=_get("gemini_model", "GEMINI_MODEL") or "gemini-2.5-flash",
            db_path=db_path,
            log_level=_get("log_level", "LOG_LEVEL") or "INFO",
            knowledge_dir=ROOT_DIR / "pos",
            enable_gemini_cache=(_get("enable_gemini_cache") or "true").lower() == "true",
            operator_chat_id=operator_chat_id,
            throttle_seconds=float(_get("throttle_seconds") or "1.2"),
            elevenlabs_key=_get("elevenlabs_key", "ELEVENLABS_KEY"),
            elevenlabs_voice_id=_get("elevenlabs_voice_id") or "EXAVITQu4vr4xnSDxMaL",
            voice_probability=float(_get("voice_probability") or "0.18"),
        )


settings = Settings.load()
