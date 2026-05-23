from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from aiogram.types import Message

logger = logging.getLogger(__name__)

# Sozlamalar — odam kabi javob ritmi
DEBOUNCE_MIN_SECONDS = 10.0   # mijoz xabaridan keyin minimal kutish
DEBOUNCE_MAX_SECONDS = 60.0   # uzun pauzaga ham chek bor
JITTER_RATIO = 0.2            # ±20% tasodifiy
COLD_START_GAP = 12.0         # birinchi marta — bot oldindan yozmagan paytda


@dataclass
class PendingTurn:
    """Bitta foydalanuvchi xabari — keyinroq Gemini'ga birlashib boriladi."""
    user_text: str
    save_text: str
    media_kind: str | None
    media_data: bytes | None
    media_mime: str | None
    message: Message
    received_at: float
    # AmoCRM Chat panelga forward qilish uchun (public URL bo'lsa)
    media_url: str | None = None
    media_size: int = 0
    media_chat_type: str | None = None  # "voice", "video", "picture" — AmoCRM type


@dataclass
class UserState:
    pending: list[PendingTurn] = field(default_factory=list)
    debounce_task: asyncio.Task | None = None
    last_user_msg_at: float = 0.0
    last_bot_msg_at: float = 0.0


ProcessCallback = Callable[[int, list[PendingTurn]], Awaitable[None]]


class PacingScheduler:
    """Mijoz xabarlarini debounce qilib batch sifatida ishlaydi.

    Har yangi xabar timer'ni qayta yuradi — mijoz to'xtaganda (yangi xabar
    kelmaganda) belgilangan kechikishdan keyin process_callback chaqiriladi.
    Kechikish minimal 10 soniya, lekin mijozning javob beruv vaqtiga moslashadi.
    """

    def __init__(
        self,
        process: ProcessCallback,
        min_seconds: float = DEBOUNCE_MIN_SECONDS,
        max_seconds: float = DEBOUNCE_MAX_SECONDS,
        immediate: bool = False,
    ) -> None:
        self._process = process
        self._min = min_seconds
        self._max = max_seconds
        self._immediate = immediate
        self._states: dict[int, UserState] = {}

    def _state(self, user_id: int) -> UserState:
        s = self._states.get(user_id)
        if s is None:
            s = UserState()
            self._states[user_id] = s
        return s

    def mark_bot_done(self, user_id: int) -> None:
        """Bot oxirgi xabarini yuborib bo'lganda chaqiriladi."""
        self._state(user_id).last_bot_msg_at = time.monotonic()

    def enqueue(self, user_id: int, turn: PendingTurn) -> None:
        state = self._state(user_id)
        state.pending.append(turn)
        state.last_user_msg_at = turn.received_at

        if state.debounce_task and not state.debounce_task.done():
            state.debounce_task.cancel()

        delay = self._compute_delay(state)
        logger.debug("pacing[%s]: %d xabar, %.1fs kutilmoqda", user_id, len(state.pending), delay)
        state.debounce_task = asyncio.create_task(
            self._fire_after(user_id, delay), name=f"pacing-{user_id}"
        )

    def cancel(self, user_id: int) -> None:
        state = self._states.get(user_id)
        if not state:
            return
        if state.debounce_task and not state.debounce_task.done():
            state.debounce_task.cancel()
        state.pending = []

    def _compute_delay(self, state: UserState) -> float:
        if self._immediate:
            return 0.0
        # Mijozning bot-dan keyingi javob berish vaqtini ko'zgu sifatida olamiz
        if state.last_bot_msg_at > 0:
            user_gap = state.last_user_msg_at - state.last_bot_msg_at
        else:
            user_gap = COLD_START_GAP
        delay = max(self._min, min(self._max, user_gap))
        jitter = 1.0 + random.uniform(-JITTER_RATIO, JITTER_RATIO)
        return max(self._min, delay * jitter)

    async def _fire_after(self, user_id: int, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return

        state = self._state(user_id)
        pending = state.pending
        state.pending = []
        state.debounce_task = None

        if not pending:
            return

        try:
            await self._process(user_id, pending)
        except Exception:
            logger.exception("pacing process xato: user=%s", user_id)
