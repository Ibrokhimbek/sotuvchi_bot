from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher

from src.ai.gemini import GeminiAgent
from src.ai.prompts import build_system_prompt
from src.bot import handlers
from src.bot.handoff import HandoffNotifier
from src.bot.throttle import ThrottleMiddleware
from src.bot.tts import ElevenLabsTTS
from src.config import settings
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
    tts = ElevenLabsTTS(
        api_key=settings.elevenlabs_key,
        voice_id=settings.elevenlabs_voice_id,
        probability=settings.voice_probability,
    )

    handlers.deps.agent = agent
    handlers.deps.storage = storage
    handlers.deps.handoff = handoff
    handlers.deps.tts = tts

    dp = Dispatcher()
    dp.message.middleware(ThrottleMiddleware(min_interval=settings.throttle_seconds))
    dp.include_router(handlers.router)

    me = await bot.get_me()
    logger.info(
        "Bot ishga tushdi: @%s | tts=%s | handoff=%s",
        me.username,
        "yoqilgan" if tts.enabled else "o'chirilgan",
        "yoqilgan" if settings.operator_chat_id else "o'chirilgan",
    )
    try:
        await dp.start_polling(bot)
    finally:
        await agent.stop_auto_refresh()


if __name__ == "__main__":
    asyncio.run(main())
