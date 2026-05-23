"""AmoCRM Chats (amojo) API client.

Bu modul Telegram bot'imizdan kelgan xabarlarni AmoCRM chats paneliga
yetkazadi va operator javoblarini qabul qiladi.

Foydalanish uchun AmoCRM support'idan quyidagi credential'lar olinishi kerak:
  - channel_id   — UUID, kanal identifikatori
  - secret       — HMAC-SHA1 imzo uchun maxfiy kalit

Birinchi marta `connect()` chaqirilganda AmoCRM `scope_id` qaytaradi —
keyingi barcha endpoint'larda shu ishlatiladi.
"""

from __future__ import annotations

import base64
import email.utils
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://amojo.amocrm.ru"


def _md5(body: bytes) -> str:
    return hashlib.md5(body).hexdigest()


def _rfc2822_date() -> str:
    return email.utils.formatdate(time.time(), usegmt=False)


def _sign(secret: str, method: str, body_md5: str, content_type: str, date: str, path: str) -> str:
    """HMAC-SHA1 imzo (lowercase hex).

    Imzolanuvchi qator:
        METHOD\\nMD5\\nCONTENT_TYPE\\nDATE\\nPATH
    """
    string_to_sign = f"{method.upper()}\n{body_md5}\n{content_type}\n{date}\n{path}"
    digest = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha1).hexdigest()
    return digest


@dataclass
class ChatUser:
    """Chat ishtirokchisi (mijoz, bot yoki operator)."""
    id: str
    name: str
    avatar: str | None = None
    phone: str | None = None
    email: str | None = None
    ref_id: str | None = None  # AmoCRM ichidagi user_id (operator/bot) — agar mavjud bo'lsa


@dataclass
class MediaInfo:
    """Media fayl ma'lumoti — AmoCRM chat panelda voice/video/picture sifatida ko'rinadi."""
    type: str  # "voice" | "video" | "picture" | "audio" | "file"
    url: str
    file_name: str
    file_size: int


@dataclass
class TextMessage:
    text: str
    type: str = "text"


class AmoCRMChatsClient:
    def __init__(
        self,
        channel_id: str | None,
        secret: str | None,
        scope_id: str | None = None,
    ) -> None:
        self._channel_id = channel_id
        self._secret = secret
        self._scope_id = scope_id  # connect()'dan keyin to'ldiriladi yoki env'dan o'qiladi
        self._http: httpx.AsyncClient | None = None

    @property
    def enabled(self) -> bool:
        """To'liq ishlashga tayyor: channel + secret + scope_id mavjud."""
        return bool(self._channel_id and self._secret and self._scope_id)

    @property
    def secret(self) -> str | None:
        return self._secret

    @property
    def scope_id(self) -> str | None:
        return self._scope_id

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=20.0)
        return self._http

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def _request(self, method: str, path: str, body: dict | list | None = None) -> dict:
        if not self._secret:
            raise RuntimeError("Chats client: secret yo'q")
        raw = b"" if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        content_type = "application/json"
        date = _rfc2822_date()
        body_md5 = _md5(raw)
        signature = _sign(self._secret, method, body_md5, content_type, date, path)

        headers = {
            "Date": date,
            "Content-Type": content_type,
            "Content-MD5": body_md5,
            "X-Signature": signature,
            "User-Agent": "linko-sotuvchi-bot/1.0",
        }
        url = f"{BASE_URL}{path}"
        resp = await self._client().request(method, url, content=raw, headers=headers)
        if resp.status_code >= 400:
            logger.error(
                "Chat API xato %s %s: %s",
                resp.status_code, path, resp.text[:400],
            )
        resp.raise_for_status()
        if not resp.content:
            return {}
        try:
            return resp.json()
        except Exception:
            return {"raw": resp.text}

    # ---------- Kanalni hisobga ulash (BIR MARTALIK) ----------

    async def connect(
        self,
        amocrm_account_id: str,
        title: str = "Linko Sotuv Telegram Bot",
        hook_api_version: str = "v2",
    ) -> str:
        """Kanalni amoCRM hisobiga ulaydi va scope_id qaytaradi.

        Bu BIR MARTALIK operatsiya — natijani .env'ga saqlang.
        """
        if not self._channel_id:
            raise RuntimeError("channel_id yo'q")
        path = f"/v2/origin/custom/{self._channel_id}/connect"
        body = {
            "account_id": amocrm_account_id,
            "title": title,
            "hook_api_version": hook_api_version,
        }
        result = await self._request("POST", path, body)
        scope_id = result.get("scope_id") or result.get("account_id")
        if not scope_id:
            raise RuntimeError(f"connect javobida scope_id yo'q: {result}")
        self._scope_id = scope_id
        return scope_id

    async def disconnect(self) -> None:
        if not self._channel_id:
            return
        path = f"/v2/origin/custom/{self._channel_id}/disconnect"
        await self._request("DELETE", path)

    # ---------- Chat va xabarlar ----------

    async def create_chat(
        self,
        *,
        conversation_id: str,
        user: ChatUser,
        source_external_id: str | None = None,
    ) -> dict:
        """Xabardan oldin chat yaratish (ixtiyoriy, send'ga avtomatik bo'lishi mumkin)."""
        self._require_scope()
        path = f"/v2/origin/custom/{self._scope_id}/chats"
        body: dict[str, Any] = {
            "conversation_id": conversation_id,
            "user": self._user_payload(user),
        }
        if source_external_id:
            body["source"] = {"external_id": source_external_id}
        return await self._request("POST", path, body)

    async def send_incoming_message(
        self,
        *,
        conversation_id: str,
        msgid: str,
        sender: ChatUser,
        text: str,
        media: MediaInfo | None = None,
        timestamp: int | None = None,
        msec_timestamp: int | None = None,
        silent: bool = False,
    ) -> dict:
        """Mijozdan kelgan xabarni AmoCRM chat panelida ko'rsatish."""
        return await self._send_event(
            event_type="new_message",
            conversation_id=conversation_id,
            msgid=msgid,
            sender=sender,
            receiver=None,
            text=text,
            media=media,
            timestamp=timestamp,
            msec_timestamp=msec_timestamp,
            silent=silent,
        )

    async def send_outgoing_message(
        self,
        *,
        conversation_id: str,
        msgid: str,
        sender: ChatUser,  # Manager identifikatsiyasi: ref_id = manager amojo_id
        receiver: ChatUser,
        text: str,
        timestamp: int | None = None,
        msec_timestamp: int | None = None,
    ) -> dict:
        """Nozimaxon AI / bot javobini mijoz chat threadiga jo'natadi.

        Pattern: sender.ref_id = MANAGER amojo_id (token egasi yoki maxsus operator),
        receiver = mijoz. Bu bot xabarini mijozning mavjud chat threadiga qo'shadi.
        """
        return await self._send_event(
            event_type="new_message",
            conversation_id=conversation_id,
            msgid=msgid,
            sender=sender,
            receiver=receiver,
            text=text,
            timestamp=timestamp,
            msec_timestamp=msec_timestamp,
            silent=True,
        )

    async def _send_event(
        self,
        *,
        event_type: str,
        conversation_id: str,
        msgid: str,
        sender: ChatUser,
        receiver: ChatUser | None,
        text: str,
        media: MediaInfo | None = None,
        timestamp: int | None,
        msec_timestamp: int | None = None,
        silent: bool,
    ) -> dict:
        self._require_scope()
        path = f"/v2/origin/custom/{self._scope_id}"

        if media:
            message_block: dict[str, Any] = {
                "type": media.type,
                "text": text or "",
                "media": media.url,
                "file_name": media.file_name,
                "file_size": media.file_size,
            }
        else:
            message_block = {"type": "text", "text": text}

        now_ms = int(time.time() * 1000)
        msec_ts = msec_timestamp or (timestamp * 1000 if timestamp else now_ms)
        sec_ts = timestamp or (msec_ts // 1000)

        payload: dict[str, Any] = {
            "timestamp": sec_ts,
            "msec_timestamp": msec_ts,
            "msgid": msgid,
            "conversation_id": conversation_id,
            "sender": self._user_payload(sender),
            "message": message_block,
            "silent": silent,
        }
        if receiver:
            payload["receiver"] = self._user_payload(receiver)
        body = {"event_type": event_type, "payload": payload}
        return await self._request("POST", path, body)

    async def typing(self, *, conversation_id: str, user: ChatUser, duration_ms: int = 5000) -> dict:
        self._require_scope()
        path = f"/v2/origin/custom/{self._scope_id}/typing"
        body = {
            "conversation_id": conversation_id,
            "sender": self._user_payload(user),
            "expires_at": int(time.time() * 1000) + duration_ms,
        }
        return await self._request("POST", path, body)

    async def delivery_status(
        self, *, msgid: str, status_code: int, error_code: int = 0, error: str = ""
    ) -> dict:
        """status_code: 1=yetkazildi, 2=o'qildi, -1=xato."""
        self._require_scope()
        path = f"/v2/origin/custom/{self._scope_id}/{msgid}/delivery_status"
        body = {"status_code": status_code, "error_code": error_code, "error": error}
        return await self._request("POST", path, body)

    # ---------- Helpers ----------

    def _require_scope(self) -> None:
        if not self._scope_id:
            raise RuntimeError(
                "scope_id yo'q. Avval connect() chaqiring yoki .env'da "
                "amocrm_chat_scope_id ni to'ldiring."
            )

    @staticmethod
    def _user_payload(u: ChatUser) -> dict:
        out: dict[str, Any] = {"id": u.id, "name": u.name}
        if u.avatar:
            out["avatar"] = u.avatar
        if u.ref_id:
            out["ref_id"] = u.ref_id
        profile: dict[str, str] = {}
        if u.phone:
            profile["phone"] = u.phone
        if u.email:
            profile["email"] = u.email
        if profile:
            out["profile"] = profile
        return out


def verify_webhook_signature(
    secret: str,
    body: bytes,
    incoming_signature: str,
) -> bool:
    """AmoCRM'dan kelgan webhook imzosini tekshirish.

    Format (incoming, AmoCRM → biz):
      HMAC-SHA1(secret_as_string, body_without_trailing_whitespace)
    AmoCRM JSON oxiriga `\\n` qo'shib jo'natadi, lekin imzo qilishda u olinmagan.
    Shuning uchun body'ni .rstrip() qilamiz.

    Outgoing (biz → AmoCRM) imzo formatidan butunlay boshqacha
    (outgoing'da `METHOD\\nMD5\\nCT\\nDate\\nPath` ishlatiladi).
    """
    msg = body.rstrip(b"\r\n\t ")
    expected = hmac.new(secret.encode("utf-8"), msg, hashlib.sha1).hexdigest()
    return hmac.compare_digest(expected, incoming_signature.strip().lower())
