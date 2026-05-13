from __future__ import annotations

import logging
import random

import httpx

logger = logging.getLogger(__name__)


class ElevenLabsTTS:
    """ElevenLabs orqali TTS. Agar key bo'lmasa, disabled — None qaytaradi.

    Foydalanish: nutqqa aylantirish kerak bo'lganda `synth(text)` chaqir.
    Audio bayt qaytadi (mp3), Telegramga `send_voice` orqali yuboriladi.
    """

    BASE_URL = "https://api.elevenlabs.io/v1/text-to-speech"

    def __init__(
        self,
        api_key: str | None,
        voice_id: str,
        model_id: str = "eleven_multilingual_v2",
        probability: float = 0.18,
    ) -> None:
        self._api_key = api_key
        self._voice_id = voice_id
        self._model_id = model_id
        self._probability = probability

    @property
    def enabled(self) -> bool:
        return bool(self._api_key and self._voice_id)

    def should_speak(self, user_sent_voice: bool) -> bool:
        if not self.enabled:
            return False
        if user_sent_voice:
            return True  # mijoz voice yozgan bo'lsa, javobni ham voice bilan
        return random.random() < self._probability

    async def synth(self, text: str) -> bytes | None:
        if not self.enabled:
            return None
        url = f"{self.BASE_URL}/{self._voice_id}"
        headers = {
            "xi-api-key": self._api_key,
            "accept": "audio/mpeg",
            "content-type": "application/json",
        }
        payload = {
            "text": text,
            "model_id": self._model_id,
            "voice_settings": {"stability": 0.55, "similarity_boost": 0.75},
        }
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(url, headers=headers, json=payload)
                resp.raise_for_status()
                return resp.content
        except Exception:
            logger.exception("ElevenLabs TTS xatosi")
            return None
