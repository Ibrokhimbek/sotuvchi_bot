from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from google import genai
from google.genai import types

from src.ai.observability import Observability

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
        # Kamida biznes turi bo'lishi shart — bo'sh {} bilan chaqirishning oldini oladi.
        "required": ["business_type"],
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


def _trace_input(history, user_text, media) -> list[dict]:
    """Langfuse uchun yengil input tasviri (media baytlarisiz)."""
    msgs = [{"role": t.role, "content": t.text} for t in history]
    cur: dict = {"role": "user", "content": user_text or ""}
    if media:
        cur["media"] = f"{len(media)} ta fayl"
    msgs.append(cur)
    return msgs


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
        observability: Observability | None = None,
    ) -> None:
        self._client = genai.Client(api_key=api_key)
        self._model = model
        self._system = system_instruction
        self._obs = observability or Observability(None, None, None)
        self._tools = [
            types.Tool(function_declarations=[_SAVE_LEAD_DECL, _REQUEST_OPERATOR_DECL])
        ]
        # Telefon/lead matnida default safety filtrlari ba'zan finish_reason=SAFETY
        # berib bo'sh javob qaytaradi — faqat YUQORI xavfni bloklaymiz.
        self._safety_settings = [
            types.SafetySetting(category=c, threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH)
            for c in (
                types.HarmCategory.HARM_CATEGORY_HARASSMENT,
                types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
                types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
                types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
            )
        ]
        # 2.5-flash thinking modeli — persona-driven sotuv oqimida fikrlash tokeni
        # latency va narx qo'shadi, sifatga deyarli ta'sir qilmaydi. O'chiramiz.
        self._thinking_config = types.ThinkingConfig(thinking_budget=0)
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

        # Model bitta javobda matn + function_call'ni BIRGA qaytarishi mumkin.
        # Matnni yo'qotmaslik uchun har iteratsiyada yig'ib boramiz.
        collected_text: list[str] = []
        tools_called: list[str] = []
        last_finish: str | None = None
        tok_in = tok_out = tok_total = tok_cached = 0

        with self._obs.generation(
            name="nozimaxon-reply",
            model=self._model,
            input=_trace_input(history, user_text, media),
            model_parameters={
                "temperature": 0.7, "top_p": 0.9,
                "max_output_tokens": 700, "thinking_budget": 0,
            },
        ) as gen:
            for _ in range(3):
                response = await self._generate(contents)
                self._log_usage(response)
                usage = getattr(response, "usage_metadata", None)
                if usage:
                    tok_in += getattr(usage, "prompt_token_count", 0) or 0
                    tok_out += getattr(usage, "candidates_token_count", 0) or 0
                    tok_total += getattr(usage, "total_token_count", 0) or 0
                    tok_cached += getattr(usage, "cached_content_token_count", 0) or 0

                candidate = response.candidates[0] if response.candidates else None
                finish_reason = getattr(candidate, "finish_reason", None) if candidate else None
                last_finish = getattr(finish_reason, "name", str(finish_reason)) if finish_reason else last_finish
                if not candidate or not candidate.content or not candidate.content.parts:
                    logger.warning(
                        "Gemini bo'sh javob qaytardi: finish_reason=%s, collected=%r",
                        finish_reason, collected_text,
                    )
                    break

                # SAFETY / RECITATION → model bloklangan, bo'sh kelishi mumkin.
                # MAX_TOKENS → matn bor lekin uzilgan, uni baribir olamiz.
                if last_finish in ("SAFETY", "RECITATION"):
                    logger.warning("Gemini javobi bloklandi: finish_reason=%s", last_finish)

                parts = candidate.content.parts

                # Matn qismlarini yig'amiz — function_call bilan birga kelsa ham yo'qotmaymiz.
                for p in parts:
                    ptext = getattr(p, "text", None)
                    if ptext and ptext.strip():
                        collected_text.append(ptext.strip())

                function_calls = [p.function_call for p in parts if p.function_call]

                if not function_calls:
                    # Model gapini tugatdi — yig'ilgan matnni qaytaramiz.
                    break

                contents.append(candidate.content)
                response_parts: list[types.Part] = []
                for fc in function_calls:
                    tools_called.append(fc.name)
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

            final_text = "\n".join(collected_text).strip()
            if not final_text:
                logger.warning("Gemini hech qanday matn bermadi — fallback ishlatilmoqda")
                final_text = "ha aytavering, eshityapman 🙂"

            if gen:
                gen.update(
                    output=final_text,
                    usage_details={
                        "input": tok_in, "output": tok_out, "total": tok_total,
                        "cache_read_input_tokens": tok_cached,
                    },
                    metadata={
                        "finish_reason": last_finish,
                        "tools_called": tools_called,
                        "cached_prompt": bool(self._cache_name),
                    },
                )
            return final_text

    @staticmethod
    def _log_usage(response) -> None:
        """Token sarfini va cache hit'ni log'ga yozadi (xarajatni kuzatish uchun)."""
        usage = getattr(response, "usage_metadata", None)
        if not usage:
            return
        logger.debug(
            "Gemini tokenlar: prompt=%s, javob=%s, cache'dan=%s, jami=%s",
            getattr(usage, "prompt_token_count", None),
            getattr(usage, "candidates_token_count", None),
            getattr(usage, "cached_content_token_count", None),
            getattr(usage, "total_token_count", None),
        )

    async def _generate(self, contents):
        """Generate_content chaqiruvi; cache xato bo'lsa fallbackga o'tib qayta yaratadi."""
        for attempt in range(2):
            config_kwargs: dict = {
                "temperature": 0.7,
                "top_p": 0.9,
                "max_output_tokens": 700,
                "thinking_config": self._thinking_config,
                "safety_settings": self._safety_settings,
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
                    # Bloklab qayta yaratamiz — aks holda keyingi so'rovlar ham
                    # cache'siz to'liq system prompt tokenini to'laydi.
                    await self.warm_cache()
                    continue
                raise
        raise RuntimeError("generate retry exhausted")
