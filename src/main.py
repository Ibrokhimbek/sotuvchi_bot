from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiohttp import web

from src.ai.gemini import GeminiAgent
from src.ai.prompts import build_system_prompt
from src.bot import handlers
from src.bot.handoff import HandoffNotifier
from src.bot.pacing import PacingScheduler
from src.bot.throttle import ThrottleMiddleware
from src.bot.webhook import AmoCRMForwarder, build_webhook_handler
from src.bot.amocrm_callback import build_amocrm_callback_handler
from src.bot.media_store import MediaStore
from src.config import settings
from src.integrations.amocrm import AmoCRMClient
from src.integrations.amocrm_chats import AmoCRMChatsClient
from src.integrations.sheets import GoogleSheetsLogger
from src.knowledge.loader import load_knowledge_base
from src.storage.db import Storage


async def _setup(dev_mode: bool):
    """Umumiy initsializatsiya — polling va webhook rejimlari uchun."""
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    logger = logging.getLogger("main")

    if dev_mode:
        logger.warning("=== DEV REJIMI — pacing/typing/delayed_greeting o'chirilgan ===")

    knowledge = load_knowledge_base(settings.knowledge_dir)
    logger.info("Bilim bazasi yuklandi: %d ta belgi", len(knowledge))

    storage = Storage(settings.db_path)
    await storage.init()

    agent = GeminiAgent(
        api_key=settings.gemini_key,
        model=settings.gemini_model,
        system_instruction=build_system_prompt(knowledge),
        enable_cache=settings.enable_gemini_cache,
    )
    await agent.warm_cache()
    agent.start_auto_refresh()

    bot = Bot(token=settings.bot_token)

    handoff = HandoffNotifier(bot, storage, settings.operator_chat_id)
    sheets = GoogleSheetsLogger(settings.gsheets_webhook_url)
    # AmoCRMClient endi faqat manager amojo_id'sini olish uchun ishlatiladi.
    # Lead/Notes yaratilmaydi — Chat API o'zi mijozlarni va suhbatlarni boshqaradi.
    amocrm = AmoCRMClient(
        subdomain=settings.amocrm_subdomain,
        access_token=settings.amocrm_token,
        pipeline_name=settings.amocrm_pipeline_name,
    )
    chats = AmoCRMChatsClient(
        channel_id=settings.amocrm_chat_channel_id,
        secret=settings.amocrm_chat_channel_secret,
        scope_id=settings.amocrm_chat_scope_id,
    )

    # Manager amojo_id'ni aniqlash — outgoing bot xabarlari shu manager nomidan ketadi
    manager_amojo_id = settings.amocrm_manager_amojo_id
    if not manager_amojo_id and amocrm.enabled and chats.enabled:
        manager_amojo_id = await amocrm.fetch_current_user_amojo_id()
        if manager_amojo_id:
            logger.info("Manager amojo_id avtomatik aniqlandi: %s", manager_amojo_id)
        else:
            logger.warning(
                ".env'da amocrm_manager_amojo_id yo'q va auto-fetch ham ishlamadi — "
                "outgoing bot xabarlari Chat API'ga ketmaydi"
            )

    # Media store — voice/video/photo fayllarini saqlab, AmoCRM Chat panelga URL beradi.
    # Faqat webhook rejimida ishlaydi (public URL bor bo'lsa).
    media_store: MediaStore | None = None
    if settings.webhook_url:
        from pathlib import Path
        media_dir = Path("data/media")
        media_store = MediaStore(
            base_dir=media_dir,
            public_url_prefix=settings.webhook_url.rstrip("/"),
        )

    pacing = PacingScheduler(process=handlers.process_batch, immediate=dev_mode)

    handlers.deps.agent = agent
    handlers.deps.storage = storage
    handlers.deps.handoff = handoff
    handlers.deps.sheets = sheets
    handlers.deps.amocrm = amocrm
    handlers.deps.chats = chats
    handlers.deps.chats_client_uuid = settings.amocrm_chat_client_uuid
    handlers.deps.chats_manager_amojo_id = manager_amojo_id
    handlers.deps.media_store = media_store
    handlers.deps.amocrm_pipeline_id = settings.amocrm_pipeline_id
    handlers.deps.amocrm_telegram_stage_id = settings.amocrm_telegram_stage_id
    handlers.deps.amocrm_chat_default_stage_id = settings.amocrm_chat_default_stage_id
    handlers.deps.pacing = pacing
    handlers.deps.delayed_greeting_seconds = (
        0.5 if dev_mode else settings.delayed_greeting_seconds
    )
    handlers.deps.dev_mode = dev_mode
    handlers.deps.followup_enabled = settings.followup_enabled
    # Dev rejimida follow-up'ni qisqa interval bilan sinash mumkin
    handlers.deps.followup_seconds = (
        15.0 if dev_mode else settings.followup_seconds
    )
    handlers.deps.followup_max_attempts = settings.followup_max_attempts

    dp = Dispatcher()
    dp.message.middleware(ThrottleMiddleware(min_interval=settings.throttle_seconds))
    dp.include_router(handlers.router)

    return logger, bot, dp, agent, amocrm, chats


async def run_polling(dev_mode: bool) -> None:
    logger, bot, dp, agent, amocrm, chats = await _setup(dev_mode)

    # Polling — webhook bo'lsa o'chirib tashlaymiz
    try:
        webhook_info = await bot.get_webhook_info()
        if webhook_info.url:
            logger.warning("Webhook topildi (%s) — uni o'chiramiz", webhook_info.url)
            await bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        logger.exception("Webhook tekshirib bo'lmadi")

    me = await bot.get_me()
    logger.info(
        "Bot ishga tushdi (POLLING): @%s | mode=%s | handoff=%s | sheets=%s | amocrm=%s | chats=%s | delayed=%ss",
        me.username,
        "DEV" if dev_mode else "PROD",
        "yoqilgan" if settings.operator_chat_id else "o'chirilgan",
        "yoqilgan" if sheets_enabled() else "o'chirilgan",
        "yoqilgan" if amocrm.enabled else "o'chirilgan",
        "yoqilgan" if chats.enabled else "o'chirilgan",
        handlers.deps.delayed_greeting_seconds,
    )
    try:
        await dp.start_polling(bot)
    finally:
        await agent.stop_auto_refresh()
        await amocrm.close()
        await chats.close()


async def run_webhook(dev_mode: bool) -> None:
    logger, bot, dp, agent, amocrm, chats = await _setup(dev_mode)

    if not settings.webhook_url:
        raise RuntimeError(
            ".env da webhook_url aniqlanmagan. Misol: webhook_url=https://abc123.ngrok.io"
        )

    full_url = settings.webhook_url.rstrip("/") + settings.webhook_path
    forwarder = AmoCRMForwarder(
        target_url=settings.amocrm_telegram_webhook_url,
        own_webhook_url=full_url,
    )

    # Telegram'ga webhook URL'ni o'rnatamiz (eskisi avtomatik almashtiriladi)
    await bot.set_webhook(
        url=full_url,
        secret_token=settings.webhook_secret or None,
        drop_pending_updates=True,
    )
    logger.info("Telegram webhook o'rnatildi: %s", full_url)

    app = web.Application()
    app.router.add_post(
        settings.webhook_path,
        build_webhook_handler(
            dp=dp, bot=bot, secret=settings.webhook_secret, forwarder=forwarder
        ),
    )

    # Media fayllarini tarqatish — AmoCRM Chat panel ushbu URL'lardan o'qiydi
    if handlers.deps.media_store and handlers.deps.media_store.enabled:
        from aiohttp.web import FileResponse, Response as _WebResponse
        from src.bot.media_store import MediaStore as _MediaStore

        async def _media_handler(request: web.Request) -> web.StreamResponse:
            store = handlers.deps.media_store
            assert isinstance(store, _MediaStore)
            filename = request.match_info["filename"]
            path = store.get_path(filename)
            if path is None:
                return _WebResponse(status=404, text="not found")
            return FileResponse(path)

        app.router.add_get("/media/{filename}", _media_handler)
        logger.info("Media route ulandi: /media/{filename}")

    # AmoCRM Chats callback (operator AmoCRM panel'ida javob yozsa)
    if chats.enabled and settings.amocrm_chat_channel_secret:
        from src.bot.handlers import cancel_pending_greeting as _cpg
        from src.bot.handlers import cancel_pending_followup as _cpf
        callback = build_amocrm_callback_handler(
            bot=bot,
            storage=handlers.deps.storage,
            secret=settings.amocrm_chat_channel_secret,
            cancel_pending_greeting=_cpg,
            cancel_pacing=handlers.deps.pacing.cancel,
            cancel_followup=_cpf,
        )
        app.router.add_post("/amocrm/chats/{scope_id}", callback)
        logger.info("AmoCRM Chats callback ulandi: /amocrm/chats/{scope_id}")

    # Healthcheck endpoint
    app.router.add_get("/healthz", lambda r: web.Response(text="ok"))

    me = await bot.get_me()
    logger.info(
        "Bot ishga tushdi (WEBHOOK): @%s | mode=%s | amocrm=%s | chats=%s | listening %s:%s",
        me.username,
        "DEV" if dev_mode else "PROD",
        "yoqilgan" if amocrm.enabled else "o'chirilgan",
        "yoqilgan" if chats.enabled else "o'chirilgan",
        settings.webhook_host, settings.webhook_port,
    )

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, settings.webhook_host, settings.webhook_port)
    try:
        await site.start()
        while True:
            await asyncio.sleep(3600)
    finally:
        await runner.cleanup()
        await forwarder.close()
        await agent.stop_auto_refresh()
        await amocrm.close()
        await chats.close()


def sheets_enabled() -> bool:
    return bool(settings.gsheets_webhook_url)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Nozimaxon — Linko-POS sotuvchi bot")
    parser.add_argument(
        "--dev",
        action="store_true",
        help="Dev rejim: pacing, typing va kechikishlar o'chirilgan",
    )
    parser.add_argument(
        "--webhook",
        action="store_true",
        help="Webhook rejimi (polling o'rniga). webhook_url .env'da bo'lishi shart",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    try:
        if args.webhook:
            asyncio.run(run_webhook(dev_mode=args.dev))
        else:
            asyncio.run(run_polling(dev_mode=args.dev))
    except KeyboardInterrupt:
        sys.exit(0)
