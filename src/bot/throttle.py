from __future__ import annotations

import time
from collections import defaultdict
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject


class ThrottleMiddleware(BaseMiddleware):
    """Bitta foydalanuvchidan haddan tashqari tez kelgan xabarlarni silent tashlaydi.

    Pacing scheduler asosiy rate kontrolni ushlaydi — bu middleware faqat
    button-mashing/script-spam'dan himoyalanish uchun (default 0.3s).
    """

    def __init__(self, min_interval: float = 0.3) -> None:
        self._min_interval = min_interval
        self._last: dict[int, float] = defaultdict(float)

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
        if now - self._last[user_id] < self._min_interval:
            return None
        self._last[user_id] = now
        return await handler(event, data)
