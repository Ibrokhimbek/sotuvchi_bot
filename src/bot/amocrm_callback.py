"""AmoCRM Chats — operator javoblarini qabul qiluvchi webhook.

URL pattern: /amocrm/chats/{scope_id}

Operator AmoCRM panelida javob yozsa, AmoCRM bu URL'ga POST qiladi. Biz:
  1) HMAC-SHA1 imzoni tekshiramiz
  2) conversation_id'dan Telegram user_id'ni ajratamiz
  3) Xabarni Telegram orqali mijozga yetkazamiz
  4) Mijozni `handed_off` qilib belgilaymiz (Nozimaxon endi javob bermaydi)
"""

from __future__ import annotations

import json
import logging
from typing import Callable, Awaitable

import httpx
from aiogram import Bot
from aiogram.types import BufferedInputFile
from aiohttp import web

from src.integrations.amocrm_chats import verify_webhook_signature
from src.storage.db import Storage

logger = logging.getLogger(__name__)

CONVERSATION_PREFIX = "tg_"


async def _deliver_to_telegram(
    *,
    bot: Bot,
    telegram_id: int,
    text: str,
    media_url: str,
    media_type: str,
    file_name: str,
) -> str:
    """Operator xabarini Telegram'ga yetkazadi (matn yoki media).

    Qaytarish: DB'ga saqlash uchun text representation.
    """
    if not media_url:
        await bot.send_message(telegram_id, text)
        return text

    # Media URL'ni yuklab olib Telegram'ga yuboramiz
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        resp = await client.get(media_url)
        resp.raise_for_status()
        data = resp.content

    fname = file_name or media_url.rsplit("/", 1)[-1] or "file"
    file = BufferedInputFile(data, filename=fname)
    caption = text or None

    if media_type == "voice":
        await bot.send_voice(telegram_id, file, caption=caption)
        return f"[voice {fname}] {text}".strip()
    if media_type == "audio":
        await bot.send_audio(telegram_id, file, caption=caption)
        return f"[audio {fname}] {text}".strip()
    if media_type == "video":
        await bot.send_video(telegram_id, file, caption=caption)
        return f"[video {fname}] {text}".strip()
    if media_type == "picture":
        await bot.send_photo(telegram_id, file, caption=caption)
        return f"[rasm {fname}] {text}".strip()
    # file/sticker/location yoki nomalum tur — document sifatida yuborish
    await bot.send_document(telegram_id, file, caption=caption)
    return f"[{media_type} {fname}] {text}".strip()


def _telegram_id_from_conversation(conversation_id: str) -> int | None:
    if not conversation_id or not conversation_id.startswith(CONVERSATION_PREFIX):
        return None
    raw = conversation_id[len(CONVERSATION_PREFIX):]
    try:
        return int(raw)
    except ValueError:
        return None


def build_amocrm_callback_handler(
    *,
    bot: Bot,
    storage: Storage,
    secret: str,
    cancel_pending_greeting: Callable[[int], None] | None = None,
    cancel_pacing: Callable[[int], Awaitable[None] | None] | None = None,
    cancel_followup: Callable[[int], None] | None = None,
):
    """aiohttp POST handler — AmoCRM operator javoblarini qabul qiladi."""

    async def handle(request: web.Request) -> web.Response:
        body = await request.read()
        sig = request.headers.get("X-Signature", "")

        if not verify_webhook_signature(secret=secret, body=body, incoming_signature=sig):
            logger.warning(
                "AmoCRM callback noto'g'ri imzo (incoming=%s, body_len=%d)",
                sig, len(body),
            )
            return web.Response(status=403, text="bad signature")

        try:
            event = json.loads(body)
        except Exception:
            logger.exception("AmoCRM callback JSON parse xato")
            return web.Response(status=400, text="bad json")

        # AmoCRM webhook v2 formati:
        #   {"account_id": ..., "message": {
        #       "receiver": {"client_id": "tg_..."},
        #       "sender": {"name": "..."},
        #       "conversation": {"client_id": "tg_..."},
        #       "message": {"type": "text", "text": "..."}
        #   }}
        outer_msg = event.get("message") or {}
        if not outer_msg:
            logger.info("Tushunarsiz event — message yo'q: %s", str(event)[:200])
            return web.Response(text="ignored")

        # Telegram ID'ni 3 ta joydan izlaymiz (eng ishonchli — receiver.client_id)
        conv_client_id = (outer_msg.get("conversation") or {}).get("client_id", "")
        receiver_client_id = (outer_msg.get("receiver") or {}).get("client_id", "")
        client_id_candidate = receiver_client_id or conv_client_id

        telegram_id = _telegram_id_from_conversation(client_id_candidate)
        if telegram_id is None:
            logger.warning(
                "Tushunarsiz client_id: receiver=%r conv=%r",
                receiver_client_id, conv_client_id,
            )
            return web.Response(text="bad client_id")

        inner_msg = outer_msg.get("message") or {}
        text = inner_msg.get("text") or ""
        media_url = inner_msg.get("media") or ""
        media_type = (inner_msg.get("type") or "text").lower()
        file_name = inner_msg.get("file_name") or ""
        sender = outer_msg.get("sender") or {}
        operator_name = sender.get("name") or "Operator"

        if not text and not media_url:
            logger.info("Bo'sh xabar — matn ham media ham yo'q, tashlab yuboramiz")
            return web.Response(text="empty")

        # Mijozni handoff qilib belgilaymiz — Nozimaxon endi avto-javob bermaydi
        try:
            await storage.mark_handoff(telegram_id)
        except Exception:
            logger.exception("mark_handoff xato")

        # Pending salomlashish, follow-up va pacing'ni bekor qilamiz (mavjud bo'lsa)
        if cancel_pending_greeting:
            try:
                cancel_pending_greeting(telegram_id)
            except Exception:
                logger.exception("cancel_pending_greeting xato")
        if cancel_followup:
            try:
                cancel_followup(telegram_id)
            except Exception:
                logger.exception("cancel_followup xato")
        if cancel_pacing:
            try:
                res = cancel_pacing(telegram_id)
                if hasattr(res, "__await__"):
                    await res
            except Exception:
                logger.exception("cancel_pacing xato")

        try:
            saved_text = await _deliver_to_telegram(
                bot=bot,
                telegram_id=telegram_id,
                text=text,
                media_url=media_url,
                media_type=media_type,
                file_name=file_name,
            )
            await storage.save_message(
                telegram_id, "model", f"[operator: {operator_name}] {saved_text}"
            )
            logger.info(
                "Operator javobi Telegram'ga yuborildi: tg=%s type=%s",
                telegram_id, media_type if media_url else "text",
            )
        except Exception:
            logger.exception("Operator javobini Telegram'ga yuborib bo'lmadi tg=%s", telegram_id)
            return web.Response(status=500, text="send failed")

        return web.Response(text="ok")

    return handle
