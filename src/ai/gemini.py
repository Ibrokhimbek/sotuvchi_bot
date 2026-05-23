from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 3600
CACHE_REFRESH_INTERVAL = 2700  # 45 daqiqa — TTL tugashidan oldin yangilash


@dataclass
class MediaPart:
    data: bytes
    mime_type: str


@dataclass
class Turn:
    role: str  # "user" yoki "model"
    text: str


LeadCallback = Callable[[dict], Awaitable[None]]
OperatorCallback = Callable[[str], Awaitable[None]]


_SAVE_LEAD_DECL = {
    "name": "save_lead",
    "description": (
        "Mijoz biznesi haqida yoki shaxsiy aloqa ma'lumotini bersa CHAQIR. "
        "Masalan: ism, do'kon turi (oziq-ovqat, kiyim, apteka), "
        "do'kon kattaligi (kichik do'kon, supermarket). Faqat real yangi ma'lumot "
        "berilganda chaqir. Bo'sh maydonlarni jo'natma."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "contact_name":  {"type": "string", "description": "Mijozning ismi"},
            "phone":         {"type": "string", "description": "Telefon raqami, +998... formatda"},
            "business_type": {"type": "string", "description": "Biznes turi: oziq-ovqat, kiyim, apteka, supermarket va h.k."},
            "store_size":    {"type": "string", "description": "Do'kon kattaligi yoki kassa soni"},
            "notes":         {"type": "string", "description": "Qo'shimcha eslatma"},
        },
    },
}

_REQUEST_OPERATOR_DECL = {
    "name": "request_operator",
    "description": (
        "Mijoz tirik odam / operator / menejer bilan gaplashmoqchi bo'lsa CHAQIR. "
        "Masalan: 'menejer bilan gaplashay', 'tirik odam', 'qo'ng'iroq qiling', "
        "'operator chaqir'. Mijoz noroziligini bildirsa ham chaqirish mumkin."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "reason": {"type": "string", "description": "Operator chaqirish sababi qisqacha"},
        },
    },
}


def _looks_like_cache_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(token in msg for token in ("cachedcontent", "cached_content", "cached content"))


class GeminiAgent:
    def __init__(
        self,
        api_key: str,
        model: str,
        system_instruction: str,
        enable_cache: bool = True,
        cache_refresh_interval: float = CACHE_REFRESH_INTERVAL,
    ) -> None:
        self._client = genai.Client(api_key=api_key)
        self._model = model
        self._system = system_instruction
        self._tools = [
            types.Tool(function_declarations=[_SAVE_LEAD_DECL, _REQUEST_OPERATOR_DECL])
        ]
        self._cache_name: str | None = None
        self._enable_cache = enable_cache
        self._refresh_interval = cache_refresh_interval
        self._refresh_task: asyncio.Task[None] | None = None
        self._cache_lock = asyncio.Lock()

    async def warm_cache(self) -> None:
        """System instruction va tools'ni Gemini cache'iga yuklaydi.

        Cache ishlatilganda generate_content'ga system_instruction va tools'ni
        QAYTA yuborib bo'lmaydi — ular cache ichida bo'lishi kerak.
        """
        if not self._enable_cache:
            return
        async with self._cache_lock:
            try:
                cache = await self._client.aio.caches.create(
                    model=self._model,
                    config=types.CreateCachedContentConfig(
                        system_instruction=self._system,
                        tools=self._tools,
                        ttl=f"{CACHE_TTL_SECONDS}s",
                    ),
                )
                self._cache_name = cache.name
                logger.info("Gemini cache yaratildi: %s", self._cache_name)
            except Exception as e:
                logger.warning("Gemini cache yaratib bo'lmadi: %s", e)
                self._cache_name = None

    async def _extend_ttl(self) -> bool:
        """Cache TTL'ini uzaytiradi — qayta yaratishdan arzonroq."""
        if not self._cache_name:
            return False
        async with self._cache_lock:
            try:
                await self._client.aio.caches.update(
                    name=self._cache_name,
                    config=types.UpdateCachedContentConfig(ttl=f"{CACHE_TTL_SECONDS}s"),
                )
                logger.debug("Gemini cache TTL uzaytirildi: %s", self._cache_name)
                return True
            except Exception as e:
                logger.warning("Cache TTL uzaytirib bo'lmadi: %s", e)
                self._cache_name = None
                return False

    def start_auto_refresh(self) -> None:
        """Background task — har `refresh_interval` soniyada TTL uzaytiradi."""
        if not self._enable_cache or self._refresh_task is not None:
            return
        self._refresh_task = asyncio.create_task(self._refresh_loop(), name="gemini-cache-refresh")
        logger.info("Cache auto-refresh boshlandi (har %.0f soniya)", self._refresh_interval)

    async def stop_auto_refresh(self) -> None:
        if self._refresh_task is None:
            return
        self._refresh_task.cancel()
        try:
            await self._refresh_task
        except (asyncio.CancelledError, Exception):
            pass
        self._refresh_task = None

    async def _refresh_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._refresh_interval)
            except asyncio.CancelledError:
                raise
            try:
                extended = await self._extend_ttl()
                if not extended:
                    await self.warm_cache()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Cache refresh loop'da kutilmagan xato")

    async def reply(
        self,
        history: list[Turn],
        user_text: str | None,
        media: list[MediaPart] | None = None,
        on_save_lead: LeadCallback | None = None,
        on_request_operator: OperatorCallback | None = None,
    ) -> str:
        contents: list[types.Content] = []
        for turn in history:
            role = "model" if turn.role == "model" else "user"
            contents.append(
                types.Content(role=role, parts=[types.Part.from_text(text=turn.text)])
            )

        current_parts: list[types.Part] = []
        if media:
            for m in media:
                current_parts.append(types.Part.from_bytes(data=m.data, mime_type=m.mime_type))
        if user_text:
            current_parts.append(types.Part.from_text(text=user_text))
        if not current_parts:
            current_parts.append(types.Part.from_text(text="(bo'sh xabar)"))
        contents.append(types.Content(role="user", parts=current_parts))

        for _ in range(3):
            response = await self._generate(contents)

            candidate = response.candidates[0] if response.candidates else None
            if not candidate or not candidate.content or not candidate.content.parts:
                return "kechirasiz, bir lahza yana yozib yuborasizmi"

            function_calls = [
                p.function_call for p in candidate.content.parts if p.function_call
            ]

            if not function_calls:
                text = (response.text or "").strip()
                return text or "kechirasiz, bir lahza yana yozib yuborasizmi"

            contents.append(candidate.content)
            response_parts: list[types.Part] = []
            for fc in function_calls:
                args = dict(fc.args or {})
                try:
                    if fc.name == "save_lead" and on_save_lead:
                        await on_save_lead(args)
                        result = {"ok": True}
                    elif fc.name == "request_operator" and on_request_operator:
                        await on_request_operator(args.get("reason", ""))
                        result = {"ok": True}
                    else:
                        result = {"ok": False, "error": "unknown tool"}
                except Exception as e:
                    logger.exception("Tool xato: %s", fc.name)
                    result = {"ok": False, "error": str(e)}
                response_parts.append(
                    types.Part.from_function_response(name=fc.name, response=result)
                )
            contents.append(types.Content(role="user", parts=response_parts))

        return "kechirasiz, biroz aralashib ketdim, savolni qaytarsangiz iltimos"

    async def _generate(self, contents):
        """Generate_content chaqiruvi; cache xato bo'lsa fallbackga o'tib qayta yaratadi."""
        for attempt in range(2):
            config_kwargs: dict = {
                "temperature": 0.95,
                "top_p": 0.95,
                "max_output_tokens": 700,
            }
            if self._cache_name:
                config_kwargs["cached_content"] = self._cache_name
            else:
                config_kwargs["system_instruction"] = self._system
                config_kwargs["tools"] = self._tools

            try:
                return await self._client.aio.models.generate_content(
                    model=self._model,
                    contents=contents,
                    config=types.GenerateContentConfig(**config_kwargs),
                )
            except Exception as e:
                if attempt == 0 and self._cache_name and _looks_like_cache_error(e):
                    logger.warning("Cache muddati tugagan ko'rinadi — qayta yaratamiz: %s", e)
                    self._cache_name = None
                    asyncio.create_task(self.warm_cache())  # background recreate
                    continue
                raise
        raise RuntimeError("generate retry exhausted")
