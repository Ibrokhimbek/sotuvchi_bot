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
    gsheets_webhook_url: str | None
    delayed_greeting_seconds: float
    followup_enabled: bool
    followup_seconds: float
    followup_max_attempts: int
    amocrm_subdomain: str | None
    amocrm_token: str | None
    amocrm_pipeline_name: str
    amocrm_account_id: str | None
    amocrm_chat_channel_id: str | None
    amocrm_chat_channel_secret: str | None
    amocrm_chat_scope_id: str | None
    amocrm_chat_client_uuid: str | None
    amocrm_manager_amojo_id: str | None
    amocrm_pipeline_id: int | None
    amocrm_telegram_stage_id: int | None
    amocrm_chat_default_stage_id: int | None  # AmoCRM yangi chat lead'larini qaerga qo'yadi (default Instagram)
    webhook_url: str | None
    webhook_path: str
    webhook_host: str
    webhook_port: int
    webhook_secret: str | None
    amocrm_telegram_webhook_url: str | None

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
            throttle_seconds=float(_get("throttle_seconds") or "0.3"),
            gsheets_webhook_url=_get("gsheets_webhook_url", "GSHEETS_WEBHOOK_URL"),
            delayed_greeting_seconds=float(_get("delayed_greeting_seconds") or "60"),
            followup_enabled=(_get("followup_enabled") or "true").lower() == "true",
            # Mijoz javob bermasa, qancha vaqtdan keyin eslatma yuboriladi (default 2 soat)
            followup_seconds=float(_get("followup_seconds") or "7200"),
            followup_max_attempts=int(_get("followup_max_attempts") or "2"),
            amocrm_subdomain=_get("amocrm_subdomain", "AMOCRM_SUBDOMAIN"),
            amocrm_token=_get("amocrm_token", "AMOCRM_TOKEN"),
            amocrm_pipeline_name=_get("amocrm_pipeline_name") or "POS New",
            amocrm_account_id=_get("amocrm_account_id"),
            amocrm_chat_channel_id=_get("amocrm_chat_channel_id"),
            amocrm_chat_channel_secret=_get("amocrm_chat_channel_secret"),
            amocrm_chat_scope_id=_get("amocrm_chat_scope_id"),
            amocrm_chat_client_uuid=_get("amocrm_chat_client_uuid"),
            amocrm_manager_amojo_id=_get("amocrm_manager_amojo_id"),
            amocrm_pipeline_id=int(_get("amocrm_pipeline_id")) if _get("amocrm_pipeline_id") else None,
            amocrm_telegram_stage_id=int(_get("amocrm_telegram_stage_id")) if _get("amocrm_telegram_stage_id") else None,
            amocrm_chat_default_stage_id=int(_get("amocrm_chat_default_stage_id")) if _get("amocrm_chat_default_stage_id") else None,
            webhook_url=_get("webhook_url", "WEBHOOK_URL"),
            webhook_path=_get("webhook_path") or "/telegram/webhook",
            webhook_host=_get("webhook_host") or "0.0.0.0",
            webhook_port=int(_get("webhook_port") or "8080"),
            webhook_secret=_get("webhook_secret", "WEBHOOK_SECRET"),
            amocrm_telegram_webhook_url=_get(
                "amocrm_telegram_webhook_url", "AMOCRM_TELEGRAM_WEBHOOK_URL"
            ),
        )


settings = Settings.load()
