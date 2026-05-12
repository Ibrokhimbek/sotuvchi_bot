from __future__ import annotations

import logging
from dataclasses import dataclass

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)


@dataclass
class MediaPart:
    data: bytes
    mime_type: str


@dataclass
class Turn:
    role: str  # "user" yoki "model"
    text: str


class GeminiAgent:
    def __init__(self, api_key: str, model: str, system_instruction: str) -> None:
        self._client = genai.Client(api_key=api_key)
        self._model = model
        self._system = system_instruction

    async def reply(
        self,
        history: list[Turn],
        user_text: str | None,
        media: list[MediaPart] | None = None,
    ) -> str:
        contents: list[types.Content] = []
        for turn in history:
            contents.append(
                types.Content(
                    role=turn.role,
                    parts=[types.Part.from_text(text=turn.text)],
                )
            )

        current_parts: list[types.Part] = []
        if media:
            for m in media:
                current_parts.append(
                    types.Part.from_bytes(data=m.data, mime_type=m.mime_type)
                )
        if user_text:
            current_parts.append(types.Part.from_text(text=user_text))
        if not current_parts:
            current_parts.append(types.Part.from_text(text="(mijoz bo'sh xabar yubordi)"))

        contents.append(types.Content(role="user", parts=current_parts))

        response = await self._client.aio.models.generate_content(
            model=self._model,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=self._system,
                temperature=0.9,
                top_p=0.95,
                max_output_tokens=600,
            ),
        )
        text = (response.text or "").strip()
        if not text:
            logger.warning("Gemini bo'sh javob qaytardi")
            text = "kechirasiz, bir lahza yana yozib yuborasizmi"
        return text
