from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher

from src.ai.gemini import GeminiAgent
from src.ai.prompts import build_system_prompt
from src.bot import handlers
from src.bot.handoff import HandoffNotifier
from src.bot.pacing import PacingScheduler
from src.bot.throttle import ThrottleMiddleware
from src.config import settings
from src.integrations.sheets import GoogleSheetsLogger
from src.knowledge.loader import load_knowledge_base
from src.storage.db import Storage


async def main() -> None:
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    logger = logging.getLogger("main")

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
    pacing = PacingScheduler(process=handlers.process_batch)

    handlers.deps.agent = agent
    handlers.deps.storage = storage
    handlers.deps.handoff = handoff
    handlers.deps.sheets = sheets
    handlers.deps.pacing = pacing
    handlers.deps.delayed_greeting_seconds = settings.delayed_greeting_seconds

    dp = Dispatcher()
    dp.message.middleware(ThrottleMiddleware(min_interval=settings.throttle_seconds))
    dp.include_router(handlers.router)

    me = await bot.get_me()
    logger.info(
        "Bot ishga tushdi: @%s | handoff=%s | sheets=%s | delayed=%ss",
        me.username,
        "yoqilgan" if settings.operator_chat_id else "o'chirilgan",
        "yoqilgan" if sheets.enabled else "o'chirilgan",
        int(settings.delayed_greeting_seconds),
    )
    try:
        await dp.start_polling(bot)
    finally:
        await agent.stop_auto_refresh()


if __name__ == "__main__":
    asyncio.run(main())
