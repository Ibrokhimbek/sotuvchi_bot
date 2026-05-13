from __future__ import annotations

import time
from collections import defaultdict
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject


class ThrottleMiddleware(BaseMiddleware):
    """Bitta foydalanuvchidan minimal interval bilan xabar qabul qiladi.

    Tezroq kelgan xabarlarni jim tashlab yuboradi (anti-spam). Birinchi marta
    haddan oshganda qisqa eslatma yuboradi.
    """

    def __init__(self, min_interval: float = 1.2, warning_text: str | None = None) -> None:
        self._min_interval = min_interval
        self._warning = warning_text or "biroz sekinroq aka, men hammasiga javob beraman 🙂"
        self._last: dict[int, float] = defaultdict(float)
        self._warned: dict[int, float] = defaultdict(float)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message) or event.from_user is None:
            return await handler(event, data)

        user_id = event.from_user.id
        now = time.monotonic()
        delta = now - self._last[user_id]
        if delta < self._min_interval:
            if now - self._warned[user_id] > 10:
                self._warned[user_id] = now
                try:
                    await event.reply(self._warning)
                except Exception:
                    pass
            return None

        self._last[user_id] = now
        return await handler(event, data)
