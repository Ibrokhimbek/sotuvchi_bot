from __future__ import annotations

import asyncio
import random
import re

from aiogram import Bot
from aiogram.enums import ChatAction

TYPING_REFRESH_SECONDS = 4.5
MIN_DELAY = 1.2
MAX_DELAY = 9.0
CHARS_PER_SECOND = 14.0


def estimate_typing_seconds(text: str) -> float:
    base = len(text) / CHARS_PER_SECOND
    jitter = random.uniform(0.7, 1.3)
    return max(MIN_DELAY, min(MAX_DELAY, base * jitter))


async def hold_typing(bot: Bot, chat_id: int, seconds: float) -> None:
    elapsed = 0.0
    while elapsed < seconds:
        await bot.send_chat_action(chat_id, ChatAction.TYPING)
        step = min(TYPING_REFRESH_SECONDS, seconds - elapsed)
        await asyncio.sleep(step)
        elapsed += step


_APOSTROPHE_WORDS = {
    "yo'q": "yoq",
    "Yo'q": "Yoq",
    "ko'p": "kop",
    "Ko'p": "Kop",
    "bo'ladi": "boladi",
    "bo'lsa": "bolsa",
    "bo'lgan": "bolgan",
    "o'rganib": "organib",
    "o'rganish": "organish",
    "o'qish": "oqish",
    "to'g'ri": "togri",
    "qo'shimcha": "qoshimcha",
    "so'ng": "song",
    "so'rang": "sorang",
}

# Toshkent og'zaki uslubi: "maydi" → "midi"
_MIDI_PATTERNS = [
    (re.compile(r"\bbo'?lmaydi\b"), "bomidi"),
    (re.compile(r"\bBo'?lmaydi\b"), "Bomidi"),
    (re.compile(r"\b(\w+?)maydi\b"), r"\1midi"),
    (re.compile(r"\b([A-ZЁЎҚҒҲ]\w*?)maydi\b"), r"\1midi"),
]

_PROTECT_PATTERNS = [
    re.compile(r"Linko-POS", re.IGNORECASE),
    re.compile(r"\d[\d\s]{2,}"),       # raqamlar (narx, telefon)
    re.compile(r"https?://\S+"),        # linklar
    re.compile(r"@\w+"),                # username
    re.compile(r"\+?\d{9,}"),           # telefon
]


def _is_protected(text: str, start: int, end: int) -> bool:
    for pat in _PROTECT_PATTERNS:
        for match in pat.finditer(text):
            if match.start() <= start and match.end() >= end:
                return True
    return False


def naturalize(text: str, probability: float = 0.55) -> str:
    """System promptdan kelgan javobni bir oz tabiiyroq qiladi.

    Asosiy ish Gemini system promptida bajariladi. Bu funksiya — qo'shimcha
    himoya qatlami: agar model haddan tashqari toza yozsa, bir nechta
    apostrofni tashlab yuboradi, "maydi" ni "midi" ga aylantiradi va
    boshlanishni kichik harf qiladi.
    """
    result = text

    # maydi → midi (Toshkent og'zaki) — har doim, lekin har biri o'z ehtimolligi bilan
    for pattern, replacement in _MIDI_PATTERNS:
        if random.random() < 0.7:
            result = pattern.sub(replacement, result)

    if random.random() > probability:
        return result

    for original, replacement in _APOSTROPHE_WORDS.items():
        if original not in result:
            continue
        if random.random() < 0.6:
            idx = result.find(original)
            if idx != -1 and not _is_protected(result, idx, idx + len(original)):
                result = result.replace(original, replacement, 1)

    if result and result[0].isupper() and random.random() < 0.25:
        if not _is_protected(result, 0, 1):
            result = result[0].lower() + result[1:]

    return result


def split_into_messages(text: str, max_parts: int = 5) -> list[str]:
    """Modelning `~~~` separatori bo'yicha xabarni alohida bo'laklarga ajratadi.

    Model PERSONA ichida har bir bo'lakni `~~~` bilan ajratishga o'rgatilgan.
    Agar separator topilmasa, butun matn bitta xabar sifatida qaytadi.
    """
    raw_parts = [p.strip() for p in text.split("~~~")]
    parts = [p for p in raw_parts if p]
    if not parts:
        return [text.strip()]
    if len(parts) <= max_parts:
        return parts
    head = parts[: max_parts - 1]
    tail = " ".join(parts[max_parts - 1 :])
    return [*head, tail]
