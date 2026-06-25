"""Langfuse LLM-kuzatuv (tracing) — ixtiyoriy, xavfsiz qatlam.

Langfuse kalitlari berilmasa yoki paket o'rnatilmagan bo'lsa — hammasi no-op
bo'ladi (bot baribir ishlayveradi). Gemini chaqiruvlari `generation()` kontekst
menejeri orqali trace qilinadi.
"""

from __future__ import annotations

import logging
from contextlib import nullcontext
from typing import Any

logger = logging.getLogger(__name__)


class Observability:
    def __init__(
        self,
        public_key: str | None,
        secret_key: str | None,
        host: str | None,
    ) -> None:
        self._client = None
        if not (public_key and secret_key):
            logger.info("Langfuse o'chirilgan (kalitlar yo'q)")
            return
        try:
            from langfuse import Langfuse  # lazy import
        except ImportError:
            logger.warning("`langfuse` paketi o'rnatilmagan — kuzatuv o'chirilgan")
            return
        try:
            self._client = Langfuse(
                public_key=public_key,
                secret_key=secret_key,
                host=host or "https://us.cloud.langfuse.com",
            )
            logger.info("Langfuse yoqildi: %s", host)
        except Exception:
            logger.exception("Langfuse init xato — kuzatuvsiz davom etamiz")
            self._client = None

    @property
    def enabled(self) -> bool:
        return self._client is not None

    def generation(self, **kwargs: Any):
        """Langfuse generation kontekst menejeri (yoki o'chiq bo'lsa null-context).

        Foydalanish:
            with obs.generation(name=..., model=..., input=...) as gen:
                ...
                if gen:
                    gen.update(output=..., usage_details={...})
        """
        if self._client is None:
            return nullcontext()
        try:
            # Langfuse v4: start_as_current_observation(as_type="generation").
            # v3 fallback: start_as_current_generation(...).
            if hasattr(self._client, "start_as_current_observation"):
                return self._client.start_as_current_observation(
                    as_type="generation", **kwargs
                )
            return self._client.start_as_current_generation(**kwargs)
        except Exception:
            logger.exception("Langfuse generation ochib bo'lmadi")
            return nullcontext()

    def flush(self) -> None:
        if self._client is not None:
            try:
                self._client.flush()
            except Exception:
                logger.exception("Langfuse flush xato")

    def shutdown(self) -> None:
        if self._client is not None:
            try:
                self._client.shutdown()
            except Exception:
                logger.exception("Langfuse shutdown xato")
