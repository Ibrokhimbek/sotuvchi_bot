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
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
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
from src.bot.media_store import MediaStore, TELEGRAM_TO_AMOCRM
from src.bot.pacing import PacingScheduler, PendingTurn
from src.integrations.amocrm import AmoCRMClient
from src.integrations.amocrm_chats import AmoCRMChatsClient, ChatUser, MediaInfo
from src.integrations.sheets import GoogleSheetsLogger
from src.storage.db import Storage

logger = logging.getLogger(__name__)

router = Router(name="nozimaxon")


class Deps:
    agent: GeminiAgent
    storage: Storage
    handoff: HandoffNotifier
    sheets: GoogleSheetsLogger
    amocrm: AmoCRMClient
    chats: AmoCRMChatsClient
    chats_client_uuid: str | None
    chats_manager_amojo_id: str | None  # outgoing bot xabarlari shu manager nomidan
    media_store: MediaStore | None
    # Yangi chat lead'larini avtomatik ko'chirish uchun (Instagram → Telegram)
    amocrm_pipeline_id: int | None = None
    amocrm_telegram_stage_id: int | None = None
    amocrm_chat_default_stage_id: int | None = None
    pacing: PacingScheduler
    delayed_greeting_seconds: float = 40.0
    dev_mode: bool = False
    # Follow-up: mijoz javob bermasa, Nozimaxon o'zi tabiiy eslatma yozadi
    followup_enabled: bool = True
    followup_seconds: float = 7200.0
    followup_max_attempts: int = 2


deps = Deps()

_pending_greetings: dict[int, asyncio.Task] = {}
_pending_followups: dict[int, asyncio.Task] = {}

# AmoCRM Chat API: tartibni saqlash uchun per-user serialization
_chat_user_locks: dict[int, asyncio.Lock] = {}
_chat_user_last_ms: dict[int, int] = {}


def _chat_lock(user_id: int) -> asyncio.Lock:
    lock = _chat_user_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _chat_user_locks[user_id] = lock
    return lock


def _next_chat_ms(user_id: int, candidate_ms: int | None = None) -> int:
    """Mijoz uchun monoton ortib boruvchi ms-timestamp qaytaradi.

    Lock ichida chaqirilishi kerak.
    """
    import time as _time
    last = _chat_user_last_ms.get(user_id, 0)
    now = candidate_ms if candidate_ms is not None else int(_time.time() * 1000)
    next_ms = max(now, last + 1)
    _chat_user_last_ms[user_id] = next_ms
    return next_ms


_UZ_WEEKDAYS = (
    "dushanba", "seshanba", "chorshanba", "payshanba", "juma", "shanba", "yakshanba"
)

# Qo'llab-quvvatlanadigan tillar
SUPPORTED_LANGS = ("uz", "ru")
DEFAULT_LANG = "uz"

# Gemini'ga beriladigan til ko'rsatmasi
_LANG_DIRECTIVE = {
    "uz": "o'zbek tilida",
    "ru": "rus tilida (на русском языке)",
}

# Statik (AI emas) xabarlarning tarjimalari
_CONTACT_REQUEST = {
    "uz": (
        "Linko-POS kompaniyasiga xush kelibsiz. Sotuv menejerimiz siz bilan "
        "bog'lanishini xohlaysizmi?\n\n"
        "Pastdagi tugmani bosib raqamingizni qoldiring — Hodimimiz tez orada o'zi yozadi 🙏"
    ),
    "ru": (
        "Добро пожаловать в компанию Linko-POS. Хотите, чтобы наш менеджер по продажам "
        "связался с вами?\n\n"
        "Нажмите кнопку ниже и оставьте свой номер — наш сотрудник скоро напишет вам 🙏"
    ),
}
_CONTACT_BUTTON = {
    "uz": "📱 Raqamimni jo'natish",
    "ru": "📱 Отправить мой номер",
}
_THANK_YOU = {
    "uz": (
        "Raqamingizni qoldirganingiz uchun rahmat! 🙏\n"
        "Iltimos, kutib turing — hodimimiz tez orada chat orqali aloqaga chiqadi!"
    ),
    "ru": (
        "Спасибо, что оставили свой номер! 🙏\n"
        "Пожалуйста, подождите — наш сотрудник скоро свяжется с вами в чате!"
    ),
}
_CHOOSE_LANG_TEXT = "Iltimos, suhbat tilini tanlang:\nПожалуйста, выберите язык общения:"
_ERROR_TECH = {
    "uz": "biroz texnik nuqson chiqdi, bir lahzadan keyin yana yozasizmi 🙏",
    "ru": "произошёл небольшой сбой, напишите ещё раз через мгновение 🙏",
}
_UNSUPPORTED_MSG = {
    "uz": "kechirasiz, bu turdagi xabarni tushunmadim 🙂 matn yoki ovozli yozsangiz boladi",
    "ru": "извините, я не поняла этот тип сообщения 🙂 напишите текстом или голосовым",
}


def _norm_lang(lang: str | None) -> str:
    return lang if lang in SUPPORTED_LANGS else DEFAULT_LANG


def _language_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🇺🇿 O'zbekcha", callback_data="lang:uz"),
        InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang:ru"),
    ]])


def _contact_keyboard(lang: str = DEFAULT_LANG) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=_CONTACT_BUTTON[_norm_lang(lang)], request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def _turn_context(first_name: str | None, lang: str = DEFAULT_LANG) -> str:
    """Har turn uchun kichik kontekst — sana, javob tili va mijoz ismi.

    System promptga emas, per-turn user content'ga qo'shiladi (caching buzilmaydi).
    Model bu ma'lumotni ko'rsatma sifatida ishlatadi, javobda takrorlamaydi.
    """
    from datetime import datetime

    now = datetime.now()
    parts = [f"bugun {now:%Y-%m-%d}, {_UZ_WEEKDAYS[now.weekday()]}"]
    parts.append(
        f"javob tili: {_LANG_DIRECTIVE[_norm_lang(lang)]} — javobni FAQAT shu tilda yoz"
    )
    if first_name:
        parts.append(f"mijoz ismi: {first_name} (iloji bo'lsa shu ism bilan murojaat qil)")
    return "[ichki kontekst — javobda takrorlama: " + "; ".join(parts) + "]"


def _cancel_pending_greeting(user_id: int) -> None:
    task = _pending_greetings.pop(user_id, None)
    if task and not task.done():
        task.cancel()


def cancel_pending_greeting(user_id: int) -> None:
    """Public alias — boshqa modullardan chaqirish uchun (masalan AmoCRM callback)."""
    _cancel_pending_greeting(user_id)


def _cancel_pending_followup(user_id: int) -> None:
    task = _pending_followups.pop(user_id, None)
    if task and not task.done():
        task.cancel()


def cancel_pending_followup(user_id: int) -> None:
    """Public alias — operator qo'lga olganda follow-up'ni to'xtatish uchun."""
    _cancel_pending_followup(user_id)


def _on_incoming(user_id: int) -> None:
    """Mijozdan yangi xabar kelganda kutilayotgan greeting/follow-up'larni bekor qiladi."""
    _cancel_pending_greeting(user_id)
    _cancel_pending_followup(user_id)


def _conversation_id(user_id: int) -> str:
    return f"tg_{user_id}"


def _client_chat_user(user_id: int, username: str | None, first_name: str | None, phone: str | None) -> ChatUser:
    return ChatUser(
        id=f"tg_{user_id}",
        name=first_name or (f"@{username}" if username else f"tg:{user_id}"),
        phone=phone,
    )


def _bot_chat_user() -> ChatUser:
    # Outgoing — manager pattern: ref_id = manager amojo_id, AmoCRM tomonida
    # bot xabari mijozning chat threadiga to'g'ri tushadi.
    return ChatUser(
        id="linko-nozimaxon-bot",
        name="Nozimaxon",
        ref_id=deps.chats_manager_amojo_id,
    )


async def _log_chat_incoming(
    *, user_id: int, username: str | None, first_name: str | None,
    phone: str | None, text: str, msgid: str,
    media: MediaInfo | None = None,
    timestamp: int | None = None,
    msec_timestamp: int | None = None,
) -> None:
    if not deps.chats.enabled:
        return
    try:
        await deps.chats.send_incoming_message(
            conversation_id=_conversation_id(user_id),
            msgid=msgid,
            sender=_client_chat_user(user_id, username, first_name, phone),
            text=text,
            media=media,
            timestamp=timestamp,
            msec_timestamp=msec_timestamp,
        )
    except Exception:
        logger.exception("Chat API incoming xato tg=%s", user_id)
        return
    # Yangi lead'ni Telegram etapiga ko'chirish (faqat mahalliy DB'da hali lead_id yo'q bo'lsa)
    asyncio.create_task(_maybe_route_lead_to_telegram_stage(user_id, phone))


async def _maybe_route_lead_to_telegram_stage(user_id: int, phone: str | None) -> None:
    """Mijoz uchun yangi yaratilgan lead'ni Instagram → Telegram etapiga ko'chiradi.

    Faqat bir marta amalga oshadi (lead_id DB'da saqlanadi). Lead 5 soniya
    kechikish bilan qidiriladi, AmoCRM yaratib ulgursin.
    """
    if not deps.amocrm.enabled or not deps.amocrm_pipeline_id:
        return
    if not deps.amocrm_telegram_stage_id or not deps.amocrm_chat_default_stage_id:
        return
    try:
        existing = await deps.storage.get_amocrm_lead_id(user_id)
        if existing:
            return  # avval ko'chirilgan
        await asyncio.sleep(5)
        lead_id = await deps.amocrm.find_recent_lead_in_stage(
            pipeline_id=deps.amocrm_pipeline_id,
            status_id=deps.amocrm_chat_default_stage_id,
            phone=phone,
            max_age_seconds=120,
        )
        if not lead_id:
            logger.info("Telegram etapiga ko'chirish uchun lead topilmadi tg=%s", user_id)
            return
        ok = await deps.amocrm.move_lead_to_stage(
            lead_id,
            pipeline_id=deps.amocrm_pipeline_id,
            status_id=deps.amocrm_telegram_stage_id,
        )
        if ok:
            await deps.storage.set_amocrm_lead_id(user_id, lead_id)
            logger.info("Lead %s Telegram etapiga ko'chirildi (tg=%s)", lead_id, user_id)
    except Exception:
        logger.exception("Lead routing xato tg=%s", user_id)


async def _log_chat_outgoing(
    *, user_id: int, username: str | None, first_name: str | None,
    phone: str | None, text: str, msgid: str,
    timestamp: int | None = None,
    msec_timestamp: int | None = None,
) -> None:
    if not deps.chats.enabled:
        return
    if not deps.chats_manager_amojo_id:
        # Bot xabarini sender.ref_id sifatida manager amojo_id kerak.
        # Yo'q bo'lsa, AmoCRM tomonida "sender: user not found" xatosi bo'ladi.
        return
    try:
        await deps.chats.send_outgoing_message(
            conversation_id=_conversation_id(user_id),
            msgid=msgid,
            sender=_bot_chat_user(),
            receiver=_client_chat_user(user_id, username, first_name, phone),
            text=text,
            timestamp=timestamp,
            msec_timestamp=msec_timestamp,
        )
    except Exception:
        logger.exception("Chat API outgoing xato tg=%s", user_id)


async def _log_chat_outgoing_parts(
    *, user_id: int, username: str | None, first_name: str | None,
    phone: str | None, text: str, base_msgid: str,
) -> None:
    """`~~~` bo'yicha har bo'lakni AmoCRM'ga ALOHIDA xabar sifatida yuboradi.

    Per-user lock ostida ishlaydi — global monoton ms-timestamp ta'minlanadi.
    """
    if not deps.chats.enabled or not deps.chats_manager_amojo_id:
        return
    parts = [p.strip() for p in text.split("~~~") if p.strip()]
    if not parts:
        return
    async with _chat_lock(user_id):
        for i, part in enumerate(parts):
            msec_ts = _next_chat_ms(user_id)
            await _log_chat_outgoing(
                user_id=user_id, username=username, first_name=first_name, phone=phone,
                text=part, msgid=f"{base_msgid}_p{i}",
                timestamp=msec_ts // 1000, msec_timestamp=msec_ts,
            )


async def _log_turn_to_chat_api(
    *, user_id: int, username: str | None, first_name: str | None,
    phone: str | None, pending: list[PendingTurn], reply_text: str, base_msgid: str,
) -> None:
    """Bitta turn ichidagi BARCHA xabarlarni AmoCRM'ga yuboradi.

    Per-user LOCK ishlatiladi — agar bir vaqtda bir nechta batch tasklari ishlasa,
    ular ketma-ket ishlaydi. Har xabarga GLOBAL monoton ms-timestamp beriladi.
    """
    if not deps.chats.enabled:
        return

    async with _chat_lock(user_id):
        # 1. Incoming — Telegram vaqti yoki monoton ortish
        for turn in pending:
            media_info = _media_info_from_turn(turn)
            tg_msg_id = getattr(turn.message, "message_id", None) if turn.message else None
            msgid = f"in_{user_id}_{tg_msg_id or int(turn.received_at * 1000)}"
            base_ms = None
            if turn.message and getattr(turn.message, "date", None):
                try:
                    base_ms = int(turn.message.date.timestamp() * 1000)
                except Exception:
                    base_ms = None
            msec_ts = _next_chat_ms(user_id, candidate_ms=base_ms)
            await _log_chat_incoming(
                user_id=user_id, username=username, first_name=first_name, phone=phone,
                text="" if media_info else (turn.save_text or "(media)"),
                msgid=msgid, media=media_info,
                timestamp=msec_ts // 1000, msec_timestamp=msec_ts,
            )

        # 2. Outgoing — `~~~` separator bo'yicha har bo'lak
        parts = [p.strip() for p in reply_text.split("~~~") if p.strip()]
        if parts and deps.chats_manager_amojo_id:
            for i, part in enumerate(parts):
                msec_ts = _next_chat_ms(user_id)
                await _log_chat_outgoing(
                    user_id=user_id, username=username, first_name=first_name, phone=phone,
                    text=part, msgid=f"{base_msgid}_p{i}",
                    timestamp=msec_ts // 1000, msec_timestamp=msec_ts,
                )


@router.message(CommandStart())
async def on_start(message: Message) -> None:
    user = message.from_user
    if user is None:
        return
    logger.info(
        "on_start: user_id=%s username=%s name=%s chat_id=%s",
        user.id, user.username, user.first_name, message.chat.id,
    )
    _on_incoming(user.id)
    deps.pacing.cancel(user.id)
    await deps.storage.upsert_user(user.id, user.username, user.first_name, user.last_name)
    await deps.storage.reset_user(user.id)
    # Avval til tanlanadi — keyin (callback'da) kontakt so'raladi
    await message.answer(_CHOOSE_LANG_TEXT, reply_markup=_language_keyboard())
    deps.pacing.mark_bot_done(user.id)


@router.callback_query(F.data.startswith("lang:"))
async def on_language_choice(callback: CallbackQuery) -> None:
    user = callback.from_user
    if user is None or not callback.data:
        return
    lang = _norm_lang(callback.data.split(":", 1)[1])
    await deps.storage.set_user_language(user.id, lang)
    logger.info("Til tanlandi: tg=%s lang=%s", user.id, lang)
    await callback.answer()
    # Til tugmalarini olib tashlaymiz (qayta bosilmasin)
    try:
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    # Tanlangan tilda kontakt so'raymiz
    if callback.message:
        await callback.message.answer(
            _CONTACT_REQUEST[lang], reply_markup=_contact_keyboard(lang)
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

    lang = await deps.storage.get_user_language(user.id)
    thank_you = _THANK_YOU[lang]
    await message.answer(thank_you, reply_markup=ReplyKeyboardRemove())
    deps.pacing.mark_bot_done(user.id)

    # Chat API: incoming (kontakt eventi) → outgoing (rahmat) — per-user lock ostida
    async def _contact_to_chat() -> None:
        async with _chat_lock(user.id):
            ms_in = _next_chat_ms(user.id)
            await _log_chat_incoming(
                user_id=user.id, username=user.username, first_name=user.first_name,
                phone=phone, text=f"[Mijoz kontaktini ulashdi: {contact_name}, {phone}]",
                msgid=f"contact_{message.message_id}",
                timestamp=ms_in // 1000, msec_timestamp=ms_in,
            )
            ms_out = _next_chat_ms(user.id)
            await _log_chat_outgoing(
                user_id=user.id, username=user.username, first_name=user.first_name,
                phone=phone, text=thank_you,
                msgid=f"thanks_{message.message_id}",
                timestamp=ms_out // 1000, msec_timestamp=ms_out,
            )
    asyncio.create_task(_contact_to_chat())

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
    logger.info("_send_delayed_greeting: user_id=%s chat_id=%s", user_id, chat_id)

    greet_row = await deps.storage.get_user_info(user_id)
    greet_name = (greet_row or {}).get("first_name")
    greet_lang = _norm_lang((greet_row or {}).get("language"))

    # MUHIM: bu mijoz bilan BIRINCHI tanishuv. History bo'sh berilgan —
    # kontakt eventi qo'shimcha kontekstga aralashtirmasligi uchun.
    reply_text = await deps.agent.reply(
        history=[],
        user_text=(
            f"{_turn_context(greet_name, greet_lang)}\n\n"
            "Bu sening mijoz bilan eng BIRINCHI tanishuv xabaring. Mijoz hozirgina "
            "Telegramda bog'lanish raqamini qoldirdi va sen unga endi yozyapsan.\n\n"
            "FAQAT QUYIDAGILARNI QIL:\n"
            "1. 'Assalomu alaykum aka!' deb iliq salomlash\n"
            "2. 'yaxshimisiz?' kabi qisqa savol\n"
            "3. O'zingni tanishtir: 'Mani ismim Nozimaxon, Linko kompaniyasidanman'\n"
            "4. Do'koni haqida BITTA umumiy savol — 'qanaqa do'kon, oziq-ovqatmi yoki boshqa yo'nalishdami?'\n\n"
            "QAT'IY TAQIQ:\n"
            "- mahsulot funksiyalari haqida (tarozi, kassa, ombor, hisobot, fiskal modul) "
            "HECH GAPIRMA\n"
            "- narx haqida hech narsa aytma\n"
            "- 'sizga juda mos keladi' kabi sotuv frazasi yozma\n"
            "- maxsulot tavsiflarini taqdim etma\n\n"
            "Bu shunchaki iliq tanishish. Mijoz o'z biznesini gapirib bersa, undan keyin "
            "boshqa suhbatlarda mahsulot haqida gaplashasan."
        ),
    )
    await deps.storage.save_message(user_id, "model", reply_text)
    await _send_parts(bot, chat_id, reply_text)
    deps.pacing.mark_bot_done(user_id)

    # Chat API outgoing — har `~~~` qism alohida xabar sifatida
    if deps.chats.enabled:
        user_row = await deps.storage.get_user_info(user_id)
        asyncio.create_task(_log_chat_outgoing_parts(
            user_id=user_id,
            username=(user_row or {}).get("username"),
            first_name=(user_row or {}).get("first_name"),
            phone=(user_row or {}).get("phone"),
            text=reply_text,
            base_msgid=f"delayed_{user_id}_{int(__import__('time').time())}",
        ))

    # Mijoz salomlashishga javob bermasa, keyinroq tabiiy eslatma yuboramiz
    _schedule_followup(user_id, bot, chat_id)


def _schedule_followup(user_id: int, bot: Bot, chat_id: int) -> None:
    """Bot mijozga yozgandan keyin chaqiriladi — javob kelmasa eslatma yuboradi.

    Bitta task butun ketma-ketlikni boshqaradi: 1-urinish `followup_seconds`dan
    keyin, har keyingisi uzayadi. Mijoz javob bersa (yoki operator qo'lga olsa)
    task bekor qilinadi.
    """
    if not deps.followup_enabled or deps.followup_max_attempts <= 0:
        return
    _cancel_pending_followup(user_id)

    async def _job() -> None:
        try:
            for attempt in range(deps.followup_max_attempts):
                delay = deps.followup_seconds * (attempt + 1)
                await asyncio.sleep(delay)
                sent = await _send_followup(user_id, bot, chat_id, attempt)
                if not sent:
                    break  # SKIP, mijoz javob berdi yoki handoff — to'xtaymiz
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Follow-up xato tg=%s", user_id)
        finally:
            _pending_followups.pop(user_id, None)

    _pending_followups[user_id] = asyncio.create_task(
        _job(), name=f"followup-{user_id}"
    )


async def _send_followup(user_id: int, bot: Bot, chat_id: int, attempt: int) -> bool:
    """Bitta follow-up xabarini yuboradi. Yuborilsa True, to'xtatish kerak bo'lsa False."""
    if await deps.storage.is_handed_off(user_id):
        return False

    history_rows = await deps.storage.recent_history(user_id, limit=30)
    if not history_rows:
        return False
    # Oxirgi yozuv mijozniki bo'lsa — u allaqachon javob bergan, eslatma shart emas.
    if history_rows[-1].role == "user":
        return False

    history = [
        Turn(role=("user" if r.role == "user" else "model"), text=r.text)
        for r in history_rows
    ]
    user_row = await deps.storage.get_user_info(user_id)
    name = (user_row or {}).get("first_name")
    lang = _norm_lang((user_row or {}).get("language"))

    if attempt == 0:
        angle = (
            "Bu BIRINCHI eslatmang. Juda yengil, qisqa qiziqish bildir — masalan "
            "'aka, oylab kordingizmi?' yoki oxirgi savolingni boshqa so'z bilan eslat."
        )
    else:
        angle = (
            "Bu OXIRGI eslatmang va sen avval allaqachon bir marta eslatib bo'lgansan "
            "(tarixdagi o'z xabaringga qara). Endi BUTUNLAY BOSHQACHA yondash — "
            "aynan o'sha gapni qaytarma. Yumshoq orqaga chekin yoki aniq qiymat taklif "
            "qil: masalan bepul demo/sinov taklif qil, yoki 'shoshilmang, tayyor "
            "bolsangiz yozing, men shu yerdaman' de. Bosim umuman yo'q."
        )

    instruction = (
        f"{_turn_context(name, lang)}\n\n"
        "Sen mijozga oxirgi xabar yozding, lekin u hali javob bermadi (bir muncha "
        "vaqt o'tdi). Sen tabiiy sotuv menejeri sifatida YENGIL eslatma yozasan — "
        "umuman bosim qilmaysan.\n\n"
        f"{angle}\n\n"
        "QOIDALAR:\n"
        "- Salomlashma — avval salomlashgansan. Darrov qisqa, do'stona eslatma.\n"
        "- MUHIM: tarixdagi o'z oldingi eslatmangni AYNAN takrorlama — boshqa so'z, "
        "boshqa ohang bilan yoz. Bir xil jumlani ikki marta yozma.\n"
        "- 1-2 ta juda qisqa xabar. Har alohida xabarni ~~~ bilan ajrat.\n"
        "- AGAR suhbat tabiiy yakuniga yetgan bo'lsa (mijoz rahmat aytib xayrlashgan, "
        "'qiziqmayman' yoki 'keyin' degan) — hech narsa yozma, FAQAT bitta so'z "
        "qaytar: SKIP"
    )

    try:
        reply_text = await deps.agent.reply(history=history, user_text=instruction)
    except Exception:
        logger.exception("Follow-up Gemini xato tg=%s", user_id)
        return False

    if not reply_text or reply_text.strip().upper().startswith("SKIP"):
        logger.info("Follow-up SKIP tg=%s attempt=%s", user_id, attempt)
        return False

    await deps.storage.save_message(user_id, "model", reply_text)
    await _send_parts(bot, chat_id, reply_text)
    deps.pacing.mark_bot_done(user_id)
    logger.info("Follow-up yuborildi tg=%s attempt=%s", user_id, attempt)

    if deps.chats.enabled:
        asyncio.create_task(_log_chat_outgoing_parts(
            user_id=user_id,
            username=(user_row or {}).get("username"),
            first_name=name,
            phone=(user_row or {}).get("phone"),
            text=reply_text,
            base_msgid=f"followup_{user_id}_{attempt}_{int(time.time())}",
        ))
    return True


@router.message(F.text)
async def on_text(message: Message) -> None:
    user = message.from_user
    if user is None or message.text is None:
        return
    _on_incoming(user.id)
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
    _on_incoming(user.id)
    data = await _download(bot, message.voice.file_id)
    await deps.storage.upsert_user(user.id, user.username, user.first_name, user.last_name)
    media_url, media_size, amocrm_type = _save_media_for_amocrm(data, "voice")
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
            media_url=media_url,
            media_size=media_size,
            media_chat_type=amocrm_type,
        ),
    )


@router.message(F.video_note)
async def on_video_note(message: Message, bot: Bot) -> None:
    user = message.from_user
    if user is None or message.video_note is None:
        return
    _on_incoming(user.id)
    data = await _download(bot, message.video_note.file_id)
    await deps.storage.upsert_user(user.id, user.username, user.first_name, user.last_name)
    media_url, media_size, amocrm_type = _save_media_for_amocrm(data, "video_note")
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
            media_url=media_url,
            media_size=media_size,
            media_chat_type=amocrm_type,
        ),
    )


@router.message(F.video)
async def on_video(message: Message, bot: Bot) -> None:
    user = message.from_user
    if user is None or message.video is None:
        return
    _on_incoming(user.id)
    data = await _download(bot, message.video.file_id)
    await deps.storage.upsert_user(user.id, user.username, user.first_name, user.last_name)
    caption = message.caption or "(mijoz video yubordi — ko'rib javob ber)"
    media_url, media_size, amocrm_type = _save_media_for_amocrm(data, "video")
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
            media_url=media_url,
            media_size=media_size,
            media_chat_type=amocrm_type,
        ),
    )


@router.message(F.photo)
async def on_photo(message: Message, bot: Bot) -> None:
    user = message.from_user
    if user is None or not message.photo:
        return
    _on_incoming(user.id)
    photo = message.photo[-1]
    data = await _download(bot, photo.file_id)
    await deps.storage.upsert_user(user.id, user.username, user.first_name, user.last_name)
    caption = message.caption or "(mijoz rasm yubordi — undagi narsani tushunib javob ber)"
    media_url, media_size, amocrm_type = _save_media_for_amocrm(data, "photo")
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
            media_url=media_url,
            media_size=media_size,
            media_chat_type=amocrm_type,
        ),
    )


@router.message()
async def on_fallback(message: Message) -> None:
    lang = DEFAULT_LANG
    if message.from_user:
        _on_incoming(message.from_user.id)
        lang = await deps.storage.get_user_language(message.from_user.id)
    await message.reply(_UNSUPPORTED_MSG[lang])


async def _download(bot: Bot, file_id: str) -> bytes:
    file = await bot.get_file(file_id)
    buf = BytesIO()
    await bot.download_file(file.file_path, buf)
    return buf.getvalue()


def _media_info_from_turn(turn: PendingTurn) -> MediaInfo | None:
    """PendingTurn'dan AmoCRM MediaInfo'sini yasaydi (URL bo'lsa)."""
    if not turn.media_url or not turn.media_chat_type:
        return None
    # URL'dan fayl nomini olamiz (.../media/<hash>.<ext>)
    file_name = turn.media_url.rsplit("/", 1)[-1]
    return MediaInfo(
        type=turn.media_chat_type,
        url=turn.media_url,
        file_name=file_name,
        file_size=turn.media_size,
    )


def _save_media_for_amocrm(
    data: bytes, telegram_kind: str,
) -> tuple[str | None, int, str | None]:
    """MediaStore'ga saqlaydi va (url, size, amocrm_type) qaytaradi.

    MediaStore yo'q yoki disabled bo'lsa, (None, 0, None) qaytaradi.
    """
    if not deps.media_store or not deps.media_store.enabled:
        return None, 0, None
    mapping = TELEGRAM_TO_AMOCRM.get(telegram_kind)
    if not mapping:
        return None, 0, None
    amocrm_type, ext = mapping
    url, size = deps.media_store.save(data, ext)
    return url, size, amocrm_type


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

    msg_user_id = last_msg.from_user.id if last_msg.from_user else None
    if msg_user_id != user_id:
        logger.error(
            "user_id mos kelmadi! pacing user_id=%s, message.from_user.id=%s, chat_id=%s",
            user_id, msg_user_id, chat_id,
        )
    logger.info(
        "process_batch: user_id=%s chat_id=%s pending=%d",
        user_id, chat_id, len(pending),
    )

    # Joriy turn xabarlari tarixga aralashmasligi uchun chegarani SAQLASHDAN OLDIN olamiz.
    history_boundary_id = await deps.storage.get_last_message_id(user_id)

    # Hammasini DB'ga yozamiz
    for turn in pending:
        await deps.storage.save_message(user_id, "user", turn.save_text, turn.media_kind)

    if await deps.storage.is_handed_off(user_id):
        # Operator boshqaryapti — Gemini chaqirilmaydi, mijozga avto-javob yo'q.
        # LEKIN mijozning xabarlarini AmoCRM Chat panel'iga yetkazamiz —
        # operator real vaqtda ko'rishi kerak.
        if deps.chats.enabled:
            user_obj = last_msg.from_user
            user_row = await deps.storage.get_user_info(user_id)
            phone_val = (user_row or {}).get("phone")
            for turn in pending:
                tg_msg_id = getattr(turn.message, "message_id", None) if turn.message else None
                msgid = f"in_{user_id}_{tg_msg_id or int(turn.received_at * 1000)}"
                media_info = _media_info_from_turn(turn)
                asyncio.create_task(_log_chat_incoming(
                    user_id=user_id,
                    username=user_obj.username if user_obj else None,
                    first_name=user_obj.first_name if user_obj else None,
                    phone=phone_val,
                    text="" if media_info else (turn.save_text or "(media)"),
                    msgid=msgid,
                    media=media_info,
                ))
        deps.pacing.mark_bot_done(user_id)
        return

    history_rows = await deps.storage.recent_history(
        user_id, limit=30, max_id=history_boundary_id
    )
    history = [
        Turn(role=("user" if r.role == "user" else "model"), text=r.text)
        for r in history_rows
    ]
    logger.info(
        "process_batch: user_id=%s history_size=%d first=%r",
        user_id, len(history), history[0].text[:60] if history else None,
    )

    media_parts: list[MediaPart] = []
    text_chunks: list[str] = []
    for turn in pending:
        if turn.media_data and turn.media_mime:
            media_parts.append(MediaPart(data=turn.media_data, mime_type=turn.media_mime))
        text_chunks.append(turn.user_text)
    combined_text = "\n".join(c for c in text_chunks if c)

    # Per-turn kontekst (sana + til + ism) — faqat modelga ko'rsatiladi, DB'ga yozilmaydi.
    ctx_user = last_msg.from_user
    ctx_lang = await deps.storage.get_user_language(user_id)
    ctx_prelude = _turn_context(ctx_user.first_name if ctx_user else None, ctx_lang)
    model_text = f"{ctx_prelude}\n{combined_text}" if combined_text else ctx_prelude

    await bot.send_chat_action(chat_id, ChatAction.TYPING)

    async def on_save_lead(args: dict) -> None:
        cleaned = {k: v for k, v in args.items() if v}
        if not cleaned:
            return
        # Sheets'ga ikki marta yozmaslik uchun: kontakt tugmasi orqali telefon
        # allaqachon saqlangan bo'lsa (on_contact yozgan), qayta yozmaymiz.
        existing = await deps.storage.get_user_info(user_id)
        already_logged = bool(existing and existing.get("phone"))

        await deps.storage.upsert_lead(user_id, **cleaned)
        if "phone" in cleaned:
            await deps.storage.set_user_phone(user_id, cleaned["phone"])

        if already_logged:
            return  # kontakt allaqachon Sheets'ga tushgan — dublikat qatordan saqlanamiz
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
            user_text=model_text,
            media=media_parts or None,
            on_save_lead=on_save_lead,
            on_request_operator=on_request_operator,
        )
    except Exception:
        logger.exception("Gemini xatosi")
        await last_msg.reply(_ERROR_TECH[ctx_lang])
        deps.pacing.mark_bot_done(user_id)
        return

    await deps.storage.save_message(user_id, "model", reply_text)
    await _send_parts(bot, chat_id, reply_text, reply_to=last_msg)
    deps.pacing.mark_bot_done(user_id)

    user_obj = last_msg.from_user
    username_val = user_obj.username if user_obj else None
    first_name_val = user_obj.first_name if user_obj else None

    # Chat API: incoming + outgoing — KETMA-KET, bitta task ichida (tartibni saqlash uchun)
    if deps.chats.enabled:
        user_row = await deps.storage.get_user_info(user_id)
        phone_val = (user_row or {}).get("phone")
        last_msg_id = getattr(last_msg, "message_id", 0)
        asyncio.create_task(_log_turn_to_chat_api(
            user_id=user_id, username=username_val, first_name=first_name_val,
            phone=phone_val, pending=pending, reply_text=reply_text,
            base_msgid=f"out_{user_id}_{last_msg_id}",
        ))

    # Mijoz bu javobga ham javob bermasa, keyinroq tabiiy eslatma yuboramiz
    _schedule_followup(user_id, bot, chat_id)


async def _send_parts(
    bot: Bot,
    chat_id: int,
    reply_text: str,
    reply_to: Message | None = None,
) -> None:
    text_parts = [naturalize(p) for p in split_into_messages(reply_text)]
    for i, part in enumerate(text_parts):
        if not deps.dev_mode:
            await hold_typing(bot, chat_id, seconds=estimate_typing_seconds(part))
        if i == 0 and reply_to is not None:
            await reply_to.reply(part)
        else:
            await bot.send_message(chat_id, part)
        if i < len(text_parts) - 1 and not deps.dev_mode:
            await asyncio.sleep(random.uniform(0.4, 1.1))
