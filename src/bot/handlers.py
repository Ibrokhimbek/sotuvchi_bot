from __future__ import annotations

import asyncio
import logging
from io import BytesIO

from aiogram import Bot, F, Router
from aiogram.filters import CommandStart
from aiogram.types import Message

from src.ai.gemini import GeminiAgent, MediaPart, Turn
from src.bot.humanize import (
    estimate_typing_seconds,
    hold_typing,
    naturalize,
    split_into_messages,
)
from src.storage.db import Storage

logger = logging.getLogger(__name__)

router = Router(name="jaloliddin")


class Deps:
    agent: GeminiAgent
    storage: Storage


deps = Deps()


@router.message(CommandStart())
async def on_start(message: Message, bot: Bot) -> None:
    user = message.from_user
    if user is None:
        return
    await deps.storage.upsert_user(
        telegram_id=user.id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
    )
    await _respond(
        bot=bot,
        message=message,
        user_text="(mijoz botni endi ochdi, /start bosdi — birinchi marta yozyapti)",
        media=None,
        media_kind=None,
        save_user_text="(start)",
    )


@router.message(F.text)
async def on_text(message: Message, bot: Bot) -> None:
    user = message.from_user
    if user is None or message.text is None:
        return
    await deps.storage.upsert_user(
        user.id, user.username, user.first_name, user.last_name
    )
    await _respond(
        bot=bot,
        message=message,
        user_text=message.text,
        media=None,
        media_kind=None,
        save_user_text=message.text,
    )


@router.message(F.voice)
async def on_voice(message: Message, bot: Bot) -> None:
    user = message.from_user
    if user is None or message.voice is None:
        return
    data = await _download(bot, message.voice.file_id)
    await deps.storage.upsert_user(
        user.id, user.username, user.first_name, user.last_name
    )
    await _respond(
        bot=bot,
        message=message,
        user_text="(mijoz ovozli xabar yubordi — tinglab javob ber)",
        media=[MediaPart(data=data, mime_type="audio/ogg")],
        media_kind="voice",
        save_user_text="[ovozli xabar]",
    )


@router.message(F.video_note)
async def on_video_note(message: Message, bot: Bot) -> None:
    user = message.from_user
    if user is None or message.video_note is None:
        return
    data = await _download(bot, message.video_note.file_id)
    await deps.storage.upsert_user(
        user.id, user.username, user.first_name, user.last_name
    )
    await _respond(
        bot=bot,
        message=message,
        user_text="(mijoz dumaloq video xabar yubordi — ko'rib, tinglab javob ber)",
        media=[MediaPart(data=data, mime_type="video/mp4")],
        media_kind="video_note",
        save_user_text="[dumaloq video]",
    )


@router.message(F.video)
async def on_video(message: Message, bot: Bot) -> None:
    user = message.from_user
    if user is None or message.video is None:
        return
    data = await _download(bot, message.video.file_id)
    await deps.storage.upsert_user(
        user.id, user.username, user.first_name, user.last_name
    )
    caption = message.caption or "(mijoz video yubordi — ko'rib javob ber)"
    await _respond(
        bot=bot,
        message=message,
        user_text=caption,
        media=[MediaPart(data=data, mime_type="video/mp4")],
        media_kind="video",
        save_user_text=f"[video] {caption}",
    )


@router.message(F.photo)
async def on_photo(message: Message, bot: Bot) -> None:
    user = message.from_user
    if user is None or not message.photo:
        return
    photo = message.photo[-1]
    data = await _download(bot, photo.file_id)
    await deps.storage.upsert_user(
        user.id, user.username, user.first_name, user.last_name
    )
    caption = message.caption or "(mijoz rasm yubordi — undagi narsani tushunib javob ber)"
    await _respond(
        bot=bot,
        message=message,
        user_text=caption,
        media=[MediaPart(data=data, mime_type="image/jpeg")],
        media_kind="photo",
        save_user_text=f"[rasm] {caption}",
    )


@router.message()
async def on_fallback(message: Message, bot: Bot) -> None:
    await message.reply("kechirasiz, bu turdagi xabarni tushunmadim 🙂 matn yoki ovozli yozsangiz bo'ladi")


async def _download(bot: Bot, file_id: str) -> bytes:
    file = await bot.get_file(file_id)
    buf = BytesIO()
    await bot.download_file(file.file_path, buf)
    return buf.getvalue()


async def _respond(
    *,
    bot: Bot,
    message: Message,
    user_text: str,
    media: list[MediaPart] | None,
    media_kind: str | None,
    save_user_text: str,
) -> None:
    user = message.from_user
    assert user is not None
    chat_id = message.chat.id

    await deps.storage.save_message(
        telegram_id=user.id,
        role="user",
        text=save_user_text,
        media_kind=media_kind,
    )

    history_rows = await deps.storage.recent_history(user.id, limit=24)
    history_rows = history_rows[:-1]  # so'nggi user xabarini olib tashlaymiz, u yangi turn
    history = [
        Turn(role=("user" if r.role == "user" else "model"), text=r.text)
        for r in history_rows
    ]

    typing_task = asyncio.create_task(
        hold_typing(bot, chat_id, seconds=estimate_typing_seconds("." * 80))
    )
    try:
        reply_text = await deps.agent.reply(history=history, user_text=user_text, media=media)
    except Exception:
        logger.exception("Gemini xatosi")
        typing_task.cancel()
        await message.reply("biroz texnik nuqson chiqdi, bir lahzadan keyin yana yozasizmi 🙏")
        return
    finally:
        typing_task.cancel()

    reply_text = naturalize(reply_text)
    await deps.storage.save_message(telegram_id=user.id, role="model", text=reply_text)

    parts = split_into_messages(reply_text)
    for i, part in enumerate(parts):
        delay = estimate_typing_seconds(part)
        await hold_typing(bot, chat_id, seconds=delay)
        if i == 0:
            await message.reply(part)
        else:
            await bot.send_message(chat_id, part)
