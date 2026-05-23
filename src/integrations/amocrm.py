from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

# Lead'larda yaratiladigan custom field'lar — logik nom → AmoCRM ko'rsatma nomi
CUSTOM_FIELDS: dict[str, str] = {
    "telegram_id": "Telegram ID",
    "telegram_username": "Telegram username",
    "contact_name": "Mijoz ismi",
    "phone": "Telefon",
    "business_type": "Biznes turi",
    "store_size": "Do'kon kattaligi",
}


def _is_lead_not_found(exc: httpx.HTTPStatusError) -> bool:
    """AmoCRM 'Lead not found' / 'Element not found' xatosini aniqlaydi.

    AmoCRM 2 ta formatda qaytaradi:
    - PATCH /leads/{id}: {"errors": {"<id>": "Lead not found"}, ...}
    - POST /leads/notes: {"errors": [{"code": 226, "message": "Error 226.", ...}]}
    """
    if exc.response.status_code != 400:
        return False
    try:
        data = exc.response.json()
    except Exception:
        return False
    errors = data.get("errors")
    if isinstance(errors, dict):
        for v in errors.values():
            if isinstance(v, str) and "not found" in v.lower():
                return True
    if isinstance(errors, list):
        for e in errors:
            if isinstance(e, dict) and e.get("code") in (226, 224):
                return True
    return False


@dataclass
class TurnPayload:
    telegram_id: int
    telegram_username: str | None
    user_first_name: str | None
    user_text: str
    bot_text: str
    existing_lead_id: int | None
    lead_fields: dict[str, str] | None = None


class AmoCRMClient:
    """AmoCRM API v4 client.

    Loyiha xizmatlari uchun mo'ljallangan: TELEGRAM voronkasida har Telegram
    foydalanuvchisi uchun bitta Lead yaratiladi, har suhbat turn'i Note sifatida
    qo'shiladi.
    """

    def __init__(
        self,
        subdomain: str | None,
        access_token: str | None,
        pipeline_name: str = "TELEGRAM",
    ) -> None:
        self._subdomain = subdomain
        self._token = access_token
        self._pipeline_name = pipeline_name
        self._base_url = (
            f"https://{subdomain}.amocrm.ru/api/v4" if subdomain else None
        )
        self._pipeline_id: int | None = None
        self._first_stage_id: int | None = None
        self._field_ids: dict[str, int] = {}
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._http: httpx.AsyncClient | None = None

    @property
    def enabled(self) -> bool:
        return bool(self._token and self._subdomain)

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=20.0,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Content-Type": "application/json",
                },
            )
        return self._http

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def init(self) -> None:
        """Bot startda chaqiriladi: voronka topiladi, custom field'lar yaratiladi."""
        if not self.enabled:
            logger.info("AmoCRM o'chirilgan — token yoki subdomain yo'q")
            return
        try:
            await self._discover_pipeline()
            await self._ensure_custom_fields()
            logger.info(
                "AmoCRM tayyor: pipeline_id=%s stage_id=%s fields=%s",
                self._pipeline_id, self._first_stage_id, list(self._field_ids),
            )
        except Exception:
            logger.exception("AmoCRM init xatosi — integratsiya o'chirildi")
            self._pipeline_id = None

    async def fetch_current_user_amojo_id(self) -> str | None:
        """Token egasining amojo_id'ni qaytaradi (manager identifikatsiyasi uchun).

        Chat API outgoing xabarlari MANAGER nomidan yuboriladi. JWT'dagi sub
        bo'yicha token egasini topib, uning amojo_id'sini qaytaramiz.
        """
        try:
            import base64, json
            # JWT'ning payload qismidan sub'ni ajratib olish
            parts = self._client_token().split(".")
            if len(parts) >= 2:
                pad = "=" * ((4 - len(parts[1]) % 4) % 4)
                payload = json.loads(base64.urlsafe_b64decode(parts[1] + pad))
                token_user_id = str(payload.get("sub", ""))
            else:
                token_user_id = ""

            resp = await self._client().get(
                f"{self._base_url}/users",
                params={"with": "amojo_id"},
            )
            resp.raise_for_status()
            users = resp.json().get("_embedded", {}).get("users", [])
            # Avval token egasini izlaymiz
            for u in users:
                if str(u.get("id", "")) == token_user_id and u.get("amojo_id"):
                    return u["amojo_id"]
            # Aks holda birinchi amojo_id'li admin
            for u in users:
                if u.get("amojo_id") and u.get("rights", {}).get("is_admin"):
                    return u["amojo_id"]
            # Eng oxiri — birinchi mavjud amojo_id
            for u in users:
                if u.get("amojo_id"):
                    return u["amojo_id"]
        except Exception:
            logger.exception("Manager amojo_id'ni olib bo'lmadi")
        return None

    def _client_token(self) -> str:
        return self._token or ""

    async def find_recent_lead_in_stage(
        self,
        *,
        pipeline_id: int,
        status_id: int,
        phone: str | None = None,
        max_age_seconds: int = 60,
        limit: int = 20,
    ) -> int | None:
        """So'nggi yaratilgan lead'ni etapdan topadi (ixtiyoriy phone bo'yicha filtr).

        Ishlatish: AmoCRM Chat API yangi chat yaratganda lead default etapda paydo
        bo'ladi (masalan Instagram). Bizning incoming jo'natganimizdan keyin bir
        necha soniya'da paydo bo'lgan lead'ni topish uchun.
        """
        try:
            params = {
                "filter[statuses][0][pipeline_id]": str(pipeline_id),
                "filter[statuses][0][status_id]": str(status_id),
                "order[id]": "desc",
                "limit": str(limit),
                "with": "contacts",
            }
            resp = await self._client().get(f"{self._base_url}/leads", params=params)
            if resp.status_code == 204:
                return None
            resp.raise_for_status()
            leads = resp.json().get("_embedded", {}).get("leads", [])
        except Exception:
            logger.exception("find_recent_lead_in_stage xato")
            return None

        import time as _time
        now = int(_time.time())
        normalized_phone = _normalize_phone(phone) if phone else None

        for lead in leads:
            created_at = lead.get("created_at", 0)
            if now - created_at > max_age_seconds:
                continue
            # Phone bo'yicha filtr (agar berilgan bo'lsa)
            if normalized_phone:
                contacts = lead.get("_embedded", {}).get("contacts", [])
                for ct in contacts:
                    ct_id = ct.get("id")
                    if not ct_id:
                        continue
                    if await self._contact_has_phone(ct_id, normalized_phone):
                        return lead["id"]
            else:
                # Phone yo'q — eng so'nggi yaroqli lead'ni qaytaramiz
                return lead["id"]
        return None

    async def _contact_has_phone(self, contact_id: int, normalized_phone: str) -> bool:
        try:
            resp = await self._client().get(f"{self._base_url}/contacts/{contact_id}")
            resp.raise_for_status()
            data = resp.json()
            for cf in data.get("custom_fields_values") or []:
                if cf.get("field_code") != "PHONE":
                    continue
                for val in cf.get("values") or []:
                    raw = val.get("value", "")
                    if _normalize_phone(raw) == normalized_phone:
                        return True
        except Exception:
            logger.exception("_contact_has_phone xato")
        return False

    async def move_lead_to_stage(
        self, lead_id: int, *, pipeline_id: int, status_id: int
    ) -> bool:
        try:
            resp = await self._client().patch(
                f"{self._base_url}/leads/{lead_id}",
                json={"pipeline_id": pipeline_id, "status_id": status_id},
            )
            resp.raise_for_status()
            return True
        except Exception:
            logger.exception("move_lead_to_stage xato lead_id=%s", lead_id)
            return False


def _normalize_phone(phone: str) -> str:
    return "".join(c for c in phone if c.isdigit())

    async def _discover_pipeline(self) -> None:
        resp = await self._client().get(f"{self._base_url}/leads/pipelines")
        resp.raise_for_status()
        pipelines = resp.json().get("_embedded", {}).get("pipelines", [])
        target = self._pipeline_name.strip().upper()
        for p in pipelines:
            if (p.get("name") or "").strip().upper() == target:
                self._pipeline_id = p["id"]
                statuses = p.get("_embedded", {}).get("statuses", [])
                # type 0 = oddiy bosqich, 1 = closed-won, 142/143 = closed
                regular = [s for s in statuses if s.get("type", 0) == 0]
                regular.sort(key=lambda s: s.get("sort", 0))
                if regular:
                    self._first_stage_id = regular[0]["id"]
                return
        raise RuntimeError(
            f"AmoCRM voronka '{self._pipeline_name}' topilmadi. "
            f"Mavjud voronkalar: {[p.get('name') for p in pipelines]}"
        )

    async def _ensure_custom_fields(self) -> None:
        resp = await self._client().get(
            f"{self._base_url}/leads/custom_fields", params={"limit": 250}
        )
        if resp.status_code == 204:
            existing = {}
        else:
            resp.raise_for_status()
            existing = {
                (f.get("name") or "").strip(): f["id"]
                for f in resp.json().get("_embedded", {}).get("custom_fields", [])
            }

        to_create: list[dict] = []
        for key, display in CUSTOM_FIELDS.items():
            if display in existing:
                self._field_ids[key] = existing[display]
            else:
                to_create.append({"name": display, "type": "text"})

        if not to_create:
            return

        resp = await self._client().post(
            f"{self._base_url}/leads/custom_fields", json=to_create
        )
        resp.raise_for_status()
        created = resp.json().get("_embedded", {}).get("custom_fields", [])
        for c in created:
            for key, display in CUSTOM_FIELDS.items():
                if c.get("name") == display and key not in self._field_ids:
                    self._field_ids[key] = c["id"]
                    break

    async def log_turn(self, payload: TurnPayload) -> int | None:
        """Suhbatning bitta turn'ini AmoCRM'ga yozadi. Lead ID qaytaradi.

        Agar mavjud lead_id endi AmoCRM'da yo'q bo'lsa (qo'l bilan o'chirilgan
        yoki boshqa hisobga ko'chirilgan), avtomatik yangi lead yaratiladi.
        """
        if not self.enabled or self._pipeline_id is None:
            return None

        async with self._locks[payload.telegram_id]:
            try:
                lead_id = payload.existing_lead_id

                # 1. Mavjud lead'ni yangilashga urinish; yo'q bo'lsa, qayta yaratish
                if lead_id is not None and payload.lead_fields:
                    try:
                        await self._update_lead_fields(lead_id, payload.lead_fields)
                    except httpx.HTTPStatusError as e:
                        if _is_lead_not_found(e):
                            logger.warning(
                                "Lead %s yo'q (o'chirilgan?) — yangisini yaratamiz",
                                lead_id,
                            )
                            lead_id = None
                        else:
                            raise

                if lead_id is None:
                    lead_id = await self._create_lead(payload)

                # 2. Note qo'shish; lead yo'q bo'lsa qayta yaratib qo'shamiz
                try:
                    await self._add_note(lead_id, payload.user_text, payload.bot_text)
                except httpx.HTTPStatusError as e:
                    if _is_lead_not_found(e):
                        logger.warning(
                            "Lead %s note qo'shishda yo'q — yangisini yaratamiz",
                            lead_id,
                        )
                        lead_id = await self._create_lead(payload)
                        await self._add_note(lead_id, payload.user_text, payload.bot_text)
                    else:
                        raise

                return lead_id
            except httpx.HTTPStatusError as e:
                logger.error(
                    "AmoCRM API xato: %s %s — %s",
                    e.response.status_code, e.request.url, e.response.text[:300],
                )
                return None
            except Exception:
                logger.exception("AmoCRM log_turn xatosi tg=%s", payload.telegram_id)
                return None

    async def _create_lead(self, p: TurnPayload) -> int:
        custom_values = self._build_custom_values({
            "telegram_id": str(p.telegram_id),
            "telegram_username": f"@{p.telegram_username}" if p.telegram_username else None,
            **(p.lead_fields or {}),
        })

        display_name = p.user_first_name or (
            f"@{p.telegram_username}" if p.telegram_username else f"tg:{p.telegram_id}"
        )
        lead: dict = {
            "name": f"Telegram: {display_name}",
            "pipeline_id": self._pipeline_id,
        }
        if self._first_stage_id is not None:
            lead["status_id"] = self._first_stage_id
        if custom_values:
            lead["custom_fields_values"] = custom_values

        resp = await self._client().post(f"{self._base_url}/leads", json=[lead])
        resp.raise_for_status()
        leads = resp.json().get("_embedded", {}).get("leads", [])
        if not leads:
            raise RuntimeError("AmoCRM lead yaratish: bo'sh javob")
        return leads[0]["id"]

    async def _update_lead_fields(self, lead_id: int, fields: dict[str, str]) -> None:
        custom_values = self._build_custom_values(fields)
        if not custom_values:
            return
        resp = await self._client().patch(
            f"{self._base_url}/leads/{lead_id}",
            json={"custom_fields_values": custom_values},
        )
        resp.raise_for_status()

    async def _add_note(self, lead_id: int, user_text: str, bot_text: str) -> None:
        # ~~~ separator'ni AmoCRM uchun yangi qatorga aylantiramiz
        bot_clean = bot_text.replace("~~~", "\n").strip()
        text = f"👤 Mijoz:\n{user_text}\n\n🟢 Nozimaxon:\n{bot_clean}"
        payload = [{
            "entity_id": lead_id,
            "note_type": "common",
            "params": {"text": text},
        }]
        resp = await self._client().post(
            f"{self._base_url}/leads/notes", json=payload
        )
        resp.raise_for_status()

    def _build_custom_values(self, fields: dict[str, str | None]) -> list[dict]:
        out: list[dict] = []
        for key, value in fields.items():
            if not value:
                continue
            field_id = self._field_ids.get(key)
            if not field_id:
                continue
            out.append({
                "field_id": field_id,
                "values": [{"value": str(value)}],
            })
        return out
