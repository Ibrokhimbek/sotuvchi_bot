"""Foydalanuvchi yuborgan media fayllarni saqlaydi va public URL beradi.

Bot Cloudflare Tunnel orqali ochiq URL'da turadi. AmoCRM Chat API media
fayllarini URL bo'yicha oladi (upload qilinmaydi), shuning uchun voice/video/
photo ni o'z server'imizdan tarqatib beramiz.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


# Telegram media → AmoCRM Chat type + fayl kengaytmasi
TELEGRAM_TO_AMOCRM = {
    "voice":      ("voice", "ogg"),
    "video_note": ("video", "mp4"),
    "video":      ("video", "mp4"),
    "photo":      ("picture", "jpg"),
}

# Eslatma: AmoCRM Chat panelda 'audio' va 'voice' har xil — voice — yumaloq UI


class MediaStore:
    """Fayllar diskka SHA256 hash bo'yicha saqlanadi (deduplication +
    obscurity). URL `{public_prefix}/media/{hash}.{ext}` ko'rinishida.
    """

    def __init__(self, base_dir: Path, public_url_prefix: str) -> None:
        self._dir = base_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._prefix = public_url_prefix.rstrip("/") if public_url_prefix else ""

    @property
    def enabled(self) -> bool:
        return bool(self._prefix)

    def save(self, data: bytes, extension: str) -> tuple[str, int]:
        """Faylni saqlaydi va (public_url, size) qaytaradi."""
        digest = hashlib.sha256(data).hexdigest()[:32]
        filename = f"{digest}.{extension}"
        path = self._dir / filename
        if not path.exists():
            path.write_bytes(data)
        url = f"{self._prefix}/media/{filename}"
        return url, len(data)

    def get_path(self, filename: str) -> Path | None:
        """Xavfsiz path lookup — path-traversaldan himoya."""
        if "/" in filename or "\\" in filename or ".." in filename:
            return None
        path = self._dir / filename
        if not path.is_file():
            return None
        return path
