from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher

from src.ai.gemini import GeminiAgent
from src.ai.prompts import build_system_prompt
from src.bot import handlers
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
    )

    handlers.deps.agent = agent
    handlers.deps.storage = storage

    bot = Bot(token=settings.bot_token)
    dp = Dispatcher()
    dp.include_router(handlers.router)

    me = await bot.get_me()
    logger.info("Bot ishga tushdi: @%s", me.username)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
