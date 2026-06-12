from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import User

from src.storage.db import Storage

logger = logging.getLogger(__name__)


class HandoffNotifier:
    """Operator chat'iga yangi lead haqida xabar yuboradi."""

    def __init__(self, bot: Bot, storage: Storage, operator_chat_id: int | None) -> None:
        self._bot = bot
        self._storage = storage
        self._operator_chat_id = operator_chat_id

    async def notify(self, user: User, reason: str) -> None:
        await self._storage.mark_handoff(user.id)
        if not self._operator_chat_id:
            logger.info("Handoff: operator_chat_id sozlanmagan, faqat DB'ga belgilandi")
            return

        lead = await self._storage.get_lead(user.id)
        try:
            history = await self._storage.recent_messages(user.id, limit=10)
        except Exception:
            history = []
        text = self._format_message(user, reason, lead, history)
        try:
            await self._bot.send_message(self._operator_chat_id, text)
        except Exception:
            logger.exception("Operator chatiga yuborib bo'lmadi")

    @staticmethod
    def _format_message(user: User, reason: str, lead: dict | None, history=None) -> str:
        name = " ".join(filter(None, [user.first_name, user.last_name])) or user.full_name
        username = f"@{user.username}" if user.username else "(username yo'q)"
        lines = [
            "🔔 Yangi lead — operator chaqirildi",
            f"Mijoz: {name} {username}",
            f"Telegram ID: {user.id}",
            f"Sabab: {reason or '—'}",
        ]
        if lead:
            for field, label in [
                ("contact_name", "Ism"),
                ("phone", "Telefon"),
                ("business_type", "Biznes turi"),
                ("store_size", "Do'kon kattaligi"),
                ("notes", "Eslatma"),
            ]:
                if lead.get(field):
                    lines.append(f"{label}: {lead[field]}")
        if history:
            lines.append("")
            lines.append("💬 Oxirgi suhbat:")
            for row in history:
                who = "Mijoz" if row.role == "user" else "Nozimaxon"
                snippet = (row.text or "").replace("\n", " ").strip()
                if len(snippet) > 200:
                    snippet = snippet[:200] + "…"
                lines.append(f"{who}: {snippet}")
        return "\n".join(lines)
