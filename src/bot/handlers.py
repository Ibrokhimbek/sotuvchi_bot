from __future__ import annotations

import asyncio
import logging
import random
import time
from io import BytesIO

from aiogram import Bot, F, Router
from aiogram.enums import ChatAction
from aiogram.filters import CommandStart
from aiogram.types import (
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from src.ai.gemini import GeminiAgent, MediaPart, Turn
from src.bot.handoff import HandoffNotifier
from src.bot.humanize import (
    estimate_typing_seconds,
    hold_typing,
    naturalize,
    split_into_messages,
)
from src.bot.pacing import PacingScheduler, PendingTurn
from src.integrations.sheets import GoogleSheetsLogger
from src.storage.db import Storage

logger = logging.getLogger(__name__)

router = Router(name="nozimaxon")


class Deps:
    agent: GeminiAgent
    storage: Storage
    handoff: HandoffNotifier
    sheets: GoogleSheetsLogger
    pacing: PacingScheduler
    delayed_greeting_seconds: float = 40.0


deps = Deps()

_pending_greetings: dict[int, asyncio.Task] = {}


def _cancel_pending_greeting(user_id: int) -> None:
    task = _pending_greetings.pop(user_id, None)
    if task and not task.done():
        task.cancel()


def _contact_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Raqamimni jo'natish", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


@router.message(CommandStart())
async def on_start(message: Message) -> None:
    user = message.from_user
    if user is None:
        return
    _cancel_pending_greeting(user.id)
    deps.pacing.cancel(user.id)
    await deps.storage.upsert_user(user.id, user.username, user.first_name, user.last_name)
    await deps.storage.reset_user(user.id)
    await message.answer(
        "Assalomu alaykum! 👋\n\n"
        "Linko-POS kompaniyasiga xush kelibsiz. Sotuv menejerimiz siz bilan "
        "bog'lanishini xohlaysizmi?\n\n"
        "Pastdagi tugmani bosib raqamingizni qoldiring — Hodimimiz tez orada o'zi yozadi 🙏",
        reply_markup=_contact_keyboard(),
    )
    deps.pacing.mark_bot_done(user.id)


@router.message(F.contact)
async def on_contact(message: Message, bot: Bot) -> None:
    user = message.from_user
    contact = message.contact
    if user is None or contact is None:
        return

    phone = contact.phone_number
    if not phone.startswith("+"):
        phone = "+" + phone.lstrip("+")

    contact_name = contact.first_name or user.first_name
    if contact.last_name:
        contact_name = f"{contact_name} {contact.last_name}".strip()

    await deps.storage.upsert_user(user.id, user.username, user.first_name, user.last_name)
    await deps.storage.set_user_phone(user.id, phone)
    await deps.storage.upsert_lead(user.id, contact_name=contact_name, phone=phone)
    await deps.storage.save_message(
        user.id, "user", f"[mijoz kontakt yubordi: {contact_name}, {phone}]", "contact"
    )

    asyncio.create_task(
        deps.sheets.log_contact(
            {
                "telegram_id": user.id,
                "username": user.username or "",
                "first_name": user.first_name or "",
                "last_name": user.last_name or "",
                "phone": phone,
                "contact_name": contact_name or "",
                "business_type": "",
                "store_size": "",
                "notes": "",
            }
        )
    )

    await message.answer(
        "Raqamingizni qoldirganingiz uchun rahmat! 🙏\n"
        "Iltimos, kutib turing — hodimimiz tez orada chat orqali aloqaga chiqadi!",
        reply_markup=ReplyKeyboardRemove(),
    )
    deps.pacing.mark_bot_done(user.id)
    _schedule_delayed_greeting(user.id, bot, message.chat.id)


def _schedule_delayed_greeting(user_id: int, bot: Bot, chat_id: int) -> None:
    _cancel_pending_greeting(user_id)

    async def _job() -> None:
        try:
            await asyncio.sleep(deps.delayed_greeting_seconds)
            await _send_delayed_greeting(user_id, bot, chat_id)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Kechikkan salomlashish xato")
        finally:
            _pending_greetings.pop(user_id, None)

    _pending_greetings[user_id] = asyncio.create_task(
        _job(), name=f"delayed-greeting-{user_id}"
    )


async def _send_delayed_greeting(user_id: int, bot: Bot, chat_id: int) -> None:
    history_rows = await deps.storage.recent_history(user_id, limit=24)
    history = [
        Turn(role=("user" if r.role == "user" else "model"), text=r.text)
        for r in history_rows
    ]

    reply_text = await deps.agent.reply(
        history=history,
        user_text=(
            "(mijoz hozirgina raqamini qoldirdi. Sen unga endi BIRINCHI marta yozayotgan "
            "Nozimaxonsan — iliq salomlash, o'zingni tanishtir va do'koni haqida so'ra)"
        ),
    )
    await deps.storage.save_message(user_id, "model", reply_text)
    await _send_parts(bot, chat_id, reply_text)
    deps.pacing.mark_bot_done(user_id)


@router.message(F.text)
async def on_text(message: Message) -> None:
    user = message.from_user
    if user is None or message.text is None:
        return
    _cancel_pending_greeting(user.id)
    await deps.storage.upsert_user(user.id, user.username, user.first_name, user.last_name)
    deps.pacing.enqueue(
        user.id,
        PendingTurn(
            user_text=message.text,
            save_text=message.text,
            media_kind=None,
            media_data=None,
            media_mime=None,
            message=message,
            received_at=time.monotonic(),
        ),
    )


@router.message(F.voice)
async def on_voice(message: Message, bot: Bot) -> None:
    user = message.from_user
    if user is None or message.voice is None:
        return
    _cancel_pending_greeting(user.id)
    data = await _download(bot, message.voice.file_id)
    await deps.storage.upsert_user(user.id, user.username, user.first_name, user.last_name)
    deps.pacing.enqueue(
        user.id,
        PendingTurn(
            user_text="(mijoz ovozli xabar yubordi — tinglab javob ber)",
            save_text="[ovozli xabar]",
            media_kind="voice",
            media_data=data,
            media_mime="audio/ogg",
            message=message,
            received_at=time.monotonic(),
        ),
    )


@router.message(F.video_note)
async def on_video_note(message: Message, bot: Bot) -> None:
    user = message.from_user
    if user is None or message.video_note is None:
        return
    _cancel_pending_greeting(user.id)
    data = await _download(bot, message.video_note.file_id)
    await deps.storage.upsert_user(user.id, user.username, user.first_name, user.last_name)
    deps.pacing.enqueue(
        user.id,
        PendingTurn(
            user_text="(mijoz dumaloq video xabar yubordi — ko'rib, tinglab javob ber)",
            save_text="[dumaloq video]",
            media_kind="video_note",
            media_data=data,
            media_mime="video/mp4",
            message=message,
            received_at=time.monotonic(),
        ),
    )


@router.message(F.video)
async def on_video(message: Message, bot: Bot) -> None:
    user = message.from_user
    if user is None or message.video is None:
        return
    _cancel_pending_greeting(user.id)
    data = await _download(bot, message.video.file_id)
    await deps.storage.upsert_user(user.id, user.username, user.first_name, user.last_name)
    caption = message.caption or "(mijoz video yubordi — ko'rib javob ber)"
    deps.pacing.enqueue(
        user.id,
        PendingTurn(
            user_text=caption,
            save_text=f"[video] {caption}",
            media_kind="video",
            media_data=data,
            media_mime="video/mp4",
            message=message,
            received_at=time.monotonic(),
        ),
    )


@router.message(F.photo)
async def on_photo(message: Message, bot: Bot) -> None:
    user = message.from_user
    if user is None or not message.photo:
        return
    _cancel_pending_greeting(user.id)
    photo = message.photo[-1]
    data = await _download(bot, photo.file_id)
    await deps.storage.upsert_user(user.id, user.username, user.first_name, user.last_name)
    caption = message.caption or "(mijoz rasm yubordi — undagi narsani tushunib javob ber)"
    deps.pacing.enqueue(
        user.id,
        PendingTurn(
            user_text=caption,
            save_text=f"[rasm] {caption}",
            media_kind="photo",
            media_data=data,
            media_mime="image/jpeg",
            message=message,
            received_at=time.monotonic(),
        ),
    )


@router.message()
async def on_fallback(message: Message) -> None:
    if message.from_user:
        _cancel_pending_greeting(message.from_user.id)
    await message.reply("kechirasiz, bu turdagi xabarni tushunmadim 🙂 matn yoki ovozli yozsangiz boladi")


async def _download(bot: Bot, file_id: str) -> bytes:
    file = await bot.get_file(file_id)
    buf = BytesIO()
    await bot.download_file(file.file_path, buf)
    return buf.getvalue()


async def process_batch(user_id: int, pending: list[PendingTurn]) -> None:
    """Pacing scheduler chaqiradi — bir nechta xabarni birga ishlaymiz."""
    if not pending:
        return

    last_msg = pending[-1].message
    bot = last_msg.bot
    chat_id = last_msg.chat.id

    if bot is None:
        logger.error("Message bot referenceisiz — javob bermaymiz")
        return

    # Hammasini DB'ga yozamiz
    for turn in pending:
        await deps.storage.save_message(user_id, "user", turn.save_text, turn.media_kind)

    if await deps.storage.is_handed_off(user_id):
        await last_msg.reply("menejerimiz tez orada o'zi bog'lanadi, biroz kuting iltimos 🙏")
        deps.pacing.mark_bot_done(user_id)
        return

    history_rows = await deps.storage.recent_history(user_id, limit=30)
    history_rows = history_rows[: -len(pending)] if len(pending) <= len(history_rows) else []
    history = [
        Turn(role=("user" if r.role == "user" else "model"), text=r.text)
        for r in history_rows
    ]

    media_parts: list[MediaPart] = []
    text_chunks: list[str] = []
    for turn in pending:
        if turn.media_data and turn.media_mime:
            media_parts.append(MediaPart(data=turn.media_data, mime_type=turn.media_mime))
        text_chunks.append(turn.user_text)
    combined_text = "\n".join(c for c in text_chunks if c)

    await bot.send_chat_action(chat_id, ChatAction.TYPING)

    async def on_save_lead(args: dict) -> None:
        cleaned = {k: v for k, v in args.items() if v}
        if not cleaned:
            return
        await deps.storage.upsert_lead(user_id, **cleaned)
        if "phone" in cleaned:
            await deps.storage.set_user_phone(user_id, cleaned["phone"])
        user = last_msg.from_user
        asyncio.create_task(
            deps.sheets.log_contact(
                {
                    "telegram_id": user_id,
                    "username": (user.username if user else "") or "",
                    "first_name": (user.first_name if user else "") or "",
                    "last_name": (user.last_name if user else "") or "",
                    "phone": cleaned.get("phone", ""),
                    "contact_name": cleaned.get("contact_name", ""),
                    "business_type": cleaned.get("business_type", ""),
                    "store_size": cleaned.get("store_size", ""),
                    "notes": cleaned.get("notes", ""),
                }
            )
        )

    async def on_request_operator(reason: str) -> None:
        user = last_msg.from_user
        if user is None:
            return
        logger.info("Handoff so'raldi: tg=%s reason=%s", user_id, reason)
        await deps.handoff.notify(user, reason)

    try:
        reply_text = await deps.agent.reply(
            history=history,
            user_text=combined_text or None,
            media=media_parts or None,
            on_save_lead=on_save_lead,
            on_request_operator=on_request_operator,
        )
    except Exception:
        logger.exception("Gemini xatosi")
        await last_msg.reply("biroz texnik nuqson chiqdi, bir lahzadan keyin yana yozasizmi 🙏")
        deps.pacing.mark_bot_done(user_id)
        return

    await deps.storage.save_message(user_id, "model", reply_text)
    await _send_parts(bot, chat_id, reply_text, reply_to=last_msg)
    deps.pacing.mark_bot_done(user_id)


async def _send_parts(
    bot: Bot,
    chat_id: int,
    reply_text: str,
    reply_to: Message | None = None,
) -> None:
    text_parts = [naturalize(p) for p in split_into_messages(reply_text)]
    for i, part in enumerate(text_parts):
        await hold_typing(bot, chat_id, seconds=estimate_typing_seconds(part))
        if i == 0 and reply_to is not None:
            await reply_to.reply(part)
        else:
            await bot.send_message(chat_id, part)
        if i < len(text_parts) - 1:
            await asyncio.sleep(random.uniform(0.4, 1.1))
