from __future__ import annotations

import asyncio
import logging

import httpx
from aiogram import Bot, Dispatcher
from aiogram.types import Update
from aiohttp import web

logger = logging.getLogger(__name__)

SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"


class AmoCRMForwarder:
    """Telegram update'larni AmoCRM webhook URL'iga yo'naltiradi (fire-and-forget)."""

    def __init__(self, target_url: str | None, own_webhook_url: str | None = None) -> None:
        # Loop himoyasi — agar AmoCRM URL bizning webhook URL bilan bir xil bo'lsa
        # forwarderni o'chirib qo'yamiz (aks holda cheksiz forward bo'ladi).
        if target_url and own_webhook_url:
            if target_url.rstrip("/") == own_webhook_url.rstrip("/"):
                logger.error(
                    "amocrm_telegram_webhook_url botning o'zining URL'iga teng — "
                    "loop oldini olish uchun forwarder O'CHIRILDI. AmoCRM'ning real "
                    "webhook URL'ini topib qo'ying."
                )
                target_url = None
        self._url = target_url
        self._client: httpx.AsyncClient | None = None

    @property
    def enabled(self) -> bool:
        return bool(self._url)

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=10.0)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def forward(self, body: bytes, content_type: str | None) -> None:
        if not self._url:
            return
        try:
            headers = {"Content-Type": content_type or "application/json"}
            resp = await self._http().post(self._url, content=body, headers=headers)
            if resp.status_code >= 400:
                logger.warning(
                    "AmoCRM forward javob: %s — %s",
                    resp.status_code, resp.text[:200],
                )
        except Exception:
            logger.exception("AmoCRM webhook'ga forward qilib bo'lmadi")


def build_webhook_handler(
    *,
    dp: Dispatcher,
    bot: Bot,
    secret: str | None,
    forwarder: AmoCRMForwarder,
):
    """aiohttp uchun POST handler — Telegram update'larini ishlaydi va AmoCRM'ga uzatadi."""

    async def handle(request: web.Request) -> web.Response:
        if secret:
            incoming = request.headers.get(SECRET_HEADER)
            if incoming != secret:
                logger.warning("Webhook noto'g'ri secret bilan keldi")
                return web.Response(status=403, text="forbidden")

        body = await request.read()
        content_type = request.headers.get("Content-Type")

        # AmoCRM ga raw body'ni fire-and-forget yuboramiz
        if forwarder.enabled:
            asyncio.create_task(forwarder.forward(body, content_type))

        # aiogram orqali ishlaymiz
        try:
            update = Update.model_validate_json(body)
        except Exception:
            logger.exception("Update parse xato")
            return web.Response(status=400, text="bad update")

        try:
            await dp.feed_update(bot=bot, update=update)
        except Exception:
            logger.exception("Update ishlash xato")
        return web.Response()

    return handle
