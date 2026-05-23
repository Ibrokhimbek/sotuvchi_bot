"""AmoCRM Chat API channel'ni hisobga ulash uchun BIR MARTALIK skript.

Ishlatish:
    1) .env'ga channel_id va secret'ni qo'shing:
       amocrm_chat_channel_id=...
       amocrm_chat_channel_secret=...

    2) Ushbu skriptni ishga tushiring:
       .venv/bin/python -m scripts.amocrm_chat_connect

    3) Chiqgan scope_id'ni .env'ga qo'shing:
       amocrm_chat_scope_id=<chiqqan_qiymat>

Bu operatsiya bir martalik — scope_id keyin doimiy ishlaydi (faqat
disconnect qilingunga qadar).
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from src.integrations.amocrm_chats import AmoCRMChatsClient  # noqa: E402


async def main() -> int:
    channel_id = os.getenv("amocrm_chat_channel_id")
    secret = os.getenv("amocrm_chat_channel_secret")
    account_id = os.getenv("amocrm_account_id")  # masalan "32040830"
    title = os.getenv("amocrm_chat_title") or "Linko Sotuv Telegram Bot"

    missing = [
        name for name, val in [
            ("amocrm_chat_channel_id", channel_id),
            ("amocrm_chat_channel_secret", secret),
            ("amocrm_account_id", account_id),
        ] if not val
    ]
    if missing:
        print(f"XATO: .env'da yo'q sozlamalar: {', '.join(missing)}")
        return 1

    client = AmoCRMChatsClient(channel_id=channel_id, secret=secret)
    try:
        print(f"Connecting channel {channel_id} to account {account_id}...")
        scope_id = await client.connect(
            amocrm_account_id=account_id, title=title, hook_api_version="v2"
        )
        print()
        print("✅ Muvaffaqiyatli ulandi!")
        print(f"scope_id = {scope_id}")
        print()
        print(".env'ga quyidagi qatorni qo'shing:")
        print(f"amocrm_chat_scope_id={scope_id}")
    except Exception as e:
        print(f"❌ Connect xato: {e}")
        return 2
    finally:
        await client.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
