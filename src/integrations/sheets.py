from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)


class GoogleSheetsLogger:
    """Google Apps Script Web App webhook'iga POST yuboradi.

    Foydalanuvchi tomonda sozlash:
    1) Google Sheets ochiladi
    2) Extensions → Apps Script → quyidagi kod joylanadi:

        function doPost(e) {
          const data = JSON.parse(e.postData.contents);
          const sheet = SpreadsheetApp.getActiveSpreadsheet().getActiveSheet();
          sheet.appendRow([
            new Date(),
            data.telegram_id, data.username, data.first_name, data.last_name,
            data.phone, data.business_type, data.store_size, data.notes,
          ]);
          return ContentService.createTextOutput('ok');
        }

    3) Deploy → New deployment → Web app
       Execute as: Me, Who has access: Anyone
    4) Hosil bo'lgan URL'ni `.env` da `gsheets_webhook_url` ga yoz.
    """

    def __init__(self, webhook_url: str | None) -> None:
        self._url = webhook_url

    @property
    def enabled(self) -> bool:
        return bool(self._url)

    async def log_contact(self, payload: dict) -> None:
        if not self._url:
            return
        try:
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                resp = await client.post(self._url, json=payload)
                resp.raise_for_status()
            logger.info("Sheets'ga yozildi: tg=%s", payload.get("telegram_id"))
        except Exception:
            logger.exception("Google Sheets webhook xatosi")
