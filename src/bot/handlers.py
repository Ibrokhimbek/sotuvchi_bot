from __future__ import annotations

import asyncio
import logging
import random
from io import BytesIO

from aiogram import Bot, F, Router
from aiogram.enums import ChatAction
from aiogram.filters import CommandStart
from aiogram.types import BufferedInputFile, Message

from src.ai.gemini import GeminiAgent, MediaPart, Turn
from src.bot.handoff import HandoffNotifier
from src.bot.humanize import (
    estimate_typing_seconds,
    hold_typing,
    naturalize,
    split_into_messages,
)
from src.bot.tts import ElevenLabsTTS
from src.storage.db import Storage

logger = logging.getLogger(__name__)

router = Router(name="nozimaxon")


class Deps:
    agent: GeminiAgent
    storage: Storage
    handoff: HandoffNotifier
    tts: ElevenLabsTTS


deps = Deps()


@router.message(CommandStart())
async def on_start(message: Message, bot: Bot) -> None:
    user = message.from_user
    if user is None:
        return
    await deps.storage.upsert_user(user.id, user.username, user.first_name, user.last_name)
    await _respond(
        bot=bot,
        message=message,
        user_text="(mijoz botni endi ochdi va /start bosdi — birinchi marta yozyapti, iliq salomlash va o'zingni tanishtir)",
        media=None,
        media_kind=None,
        save_user_text="(start)",
        user_sent_voice=False,
    )


@router.message(F.text)
async def on_text(message: Message, bot: Bot) -> None:
    user = message.from_user
    if user is None or message.text is None:
        return
    await deps.storage.upsert_user(user.id, user.username, user.first_name, user.last_name)
    await _respond(
        bot=bot,
        message=message,
        user_text=message.text,
        media=None,
        media_kind=None,
        save_user_text=message.text,
        user_sent_voice=False,
    )


@router.message(F.voice)
async def on_voice(message: Message, bot: Bot) -> None:
    user = message.from_user
    if user is None or message.voice is None:
        return
    data = await _download(bot, message.voice.file_id)
    await deps.storage.upsert_user(user.id, user.username, user.first_name, user.last_name)
    await _respond(
        bot=bot,
        message=message,
        user_text="(mijoz ovozli xabar yubordi — tinglab javob ber)",
        media=[MediaPart(data=data, mime_type="audio/ogg")],
        media_kind="voice",
        save_user_text="[ovozli xabar]",
        user_sent_voice=True,
    )


@router.message(F.video_note)
async def on_video_note(message: Message, bot: Bot) -> None:
    user = message.from_user
    if user is None or message.video_note is None:
        return
    data = await _download(bot, message.video_note.file_id)
    await deps.storage.upsert_user(user.id, user.username, user.first_name, user.last_name)
    await _respond(
        bot=bot,
        message=message,
        user_text="(mijoz dumaloq video xabar yubordi — ko'rib, tinglab javob ber)",
        media=[MediaPart(data=data, mime_type="video/mp4")],
        media_kind="video_note",
        save_user_text="[dumaloq video]",
        user_sent_voice=False,
    )


@router.message(F.video)
async def on_video(message: Message, bot: Bot) -> None:
    user = message.from_user
    if user is None or message.video is None:
        return
    data = await _download(bot, message.video.file_id)
    await deps.storage.upsert_user(user.id, user.username, user.first_name, user.last_name)
    caption = message.caption or "(mijoz video yubordi — ko'rib javob ber)"
    await _respond(
        bot=bot,
        message=message,
        user_text=caption,
        media=[MediaPart(data=data, mime_type="video/mp4")],
        media_kind="video",
        save_user_text=f"[video] {caption}",
        user_sent_voice=False,
    )


@router.message(F.photo)
async def on_photo(message: Message, bot: Bot) -> None:
    user = message.from_user
    if user is None or not message.photo:
        return
    photo = message.photo[-1]
    data = await _download(bot, photo.file_id)
    await deps.storage.upsert_user(user.id, user.username, user.first_name, user.last_name)
    caption = message.caption or "(mijoz rasm yubordi — undagi narsani tushunib javob ber)"
    await _respond(
        bot=bot,
        message=message,
        user_text=caption,
        media=[MediaPart(data=data, mime_type="image/jpeg")],
        media_kind="photo",
        save_user_text=f"[rasm] {caption}",
        user_sent_voice=False,
    )


@router.message()
async def on_fallback(message: Message) -> None:
    await message.reply("kechirasiz, bu turdagi xabarni tushunmadim 🙂 matn yoki ovozli yozsangiz boladi")


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
    user_sent_voice: bool,
) -> None:
    user = message.from_user
    assert user is not None
    chat_id = message.chat.id

    if await deps.storage.is_handed_off(user.id):
        # Operatorga uzatilgan — bot endi javob bermaydi, faqat tinch eslatma
        await deps.storage.save_message(user.id, "user", save_user_text, media_kind)
        await message.reply("menejerimiz tez orada o'zi bog'lanadi, biroz kuting iltimos 🙏")
        return

    await deps.storage.save_message(user.id, "user", save_user_text, media_kind)

    history_rows = await deps.storage.recent_history(user.id, limit=24)
    history_rows = history_rows[:-1]
    history = [
        Turn(role=("user" if r.role == "user" else "model"), text=r.text)
        for r in history_rows
    ]

    await bot.send_chat_action(chat_id, ChatAction.TYPING)

    async def on_save_lead(args: dict) -> None:
        cleaned = {k: v for k, v in args.items() if v}
        if not cleaned:
            return
        await deps.storage.upsert_lead(user.id, **cleaned)
        logger.info("Lead yangilandi: tg=%s fields=%s", user.id, list(cleaned.keys()))

    async def on_request_operator(reason: str) -> None:
        logger.info("Handoff so'raldi: tg=%s reason=%s", user.id, reason)
        await deps.handoff.notify(user, reason)

    try:
        reply_text = await deps.agent.reply(
            history=history,
            user_text=user_text,
            media=media,
            on_save_lead=on_save_lead,
            on_request_operator=on_request_operator,
        )
    except Exception:
        logger.exception("Gemini xatosi")
        await message.reply("biroz texnik nuqson chiqdi, bir lahzadan keyin yana yozasizmi 🙏")
        return

    await deps.storage.save_message(user.id, "model", reply_text)

    parts = split_into_messages(reply_text)
    speak_voice = deps.tts.should_speak(user_sent_voice=user_sent_voice) and not media_kind

    for i, part in enumerate(parts):
        part = naturalize(part)
        await hold_typing(bot, chat_id, seconds=estimate_typing_seconds(part))

        if i == 0:
            await message.reply(part)
        else:
            await bot.send_message(chat_id, part)

        if i < len(parts) - 1:
            await asyncio.sleep(random.uniform(0.4, 1.1))

    if speak_voice:
        audio = await deps.tts.synth(" ".join(parts))
        if audio:
            await bot.send_voice(chat_id, BufferedInputFile(audio, filename="reply.ogg"))
