from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    telegram_id     INTEGER PRIMARY KEY,
    username        TEXT,
    first_name      TEXT,
    last_name       TEXT,
    phone           TEXT,
    handed_off      INTEGER DEFAULT 0,    -- 1 bo'lsa operatorga uzatilgan
    amocrm_lead_id  INTEGER,              -- AmoCRM lead ID
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id   INTEGER NOT NULL,
    role          TEXT NOT NULL,         -- 'user' yoki 'model'
    text          TEXT NOT NULL,
    media_kind    TEXT,                  -- 'voice' | 'video_note' | 'photo' | NULL
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (telegram_id) REFERENCES users(telegram_id)
);

CREATE TABLE IF NOT EXISTS leads (
    telegram_id     INTEGER PRIMARY KEY,
    contact_name    TEXT,
    phone           TEXT,
    business_type   TEXT,
    store_size      TEXT,
    notes           TEXT,
    status          TEXT DEFAULT 'new',  -- new, qualified, contacted, converted
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (telegram_id) REFERENCES users(telegram_id)
);

CREATE INDEX IF NOT EXISTS idx_messages_user_time
    ON messages(telegram_id, created_at);
"""


@dataclass
class HistoryRow:
    role: str
    text: str


class Storage:
    def __init__(self, db_path: Path) -> None:
        self._path = db_path

    async def init(self) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.executescript(SCHEMA)
            await self._migrate(db)
            await db.commit()

    async def _migrate(self, db: aiosqlite.Connection) -> None:
        """Eski DB fayllar uchun yo'q ustun/jadvallarni qo'shadi."""
        cursor = await db.execute("PRAGMA table_info(users)")
        cols = {row[1] for row in await cursor.fetchall()}
        if "handed_off" not in cols:
            await db.execute("ALTER TABLE users ADD COLUMN handed_off INTEGER DEFAULT 0")
        if "amocrm_lead_id" not in cols:
            await db.execute("ALTER TABLE users ADD COLUMN amocrm_lead_id INTEGER")

    async def upsert_user(
        self,
        telegram_id: int,
        username: str | None,
        first_name: str | None,
        last_name: str | None,
    ) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                INSERT INTO users (telegram_id, username, first_name, last_name)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET
                    username = excluded.username,
                    first_name = excluded.first_name,
                    last_name = excluded.last_name
                """,
                (telegram_id, username, first_name, last_name),
            )
            await db.commit()

    async def save_message(
        self,
        telegram_id: int,
        role: str,
        text: str,
        media_kind: str | None = None,
    ) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "INSERT INTO messages (telegram_id, role, text, media_kind) VALUES (?, ?, ?, ?)",
                (telegram_id, role, text, media_kind),
            )
            await db.commit()

    async def get_last_message_id(self, telegram_id: int) -> int | None:
        """Mijozning hozirgi eng katta message id'sini qaytaradi (chegara sifatida)."""
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                "SELECT MAX(id) FROM messages WHERE telegram_id = ?",
                (telegram_id,),
            )
            row = await cursor.fetchone()
        return int(row[0]) if row and row[0] is not None else None

    async def recent_history(
        self, telegram_id: int, limit: int = 20, max_id: int | None = None
    ) -> list[HistoryRow]:
        """Oxirgi xabarlar (eskidan yangiga). `max_id` berilsa, id <= max_id bo'lganlar.

        `max_id` — batch saqlashdan OLDIN olingan chegara. Bu joriy turn xabarlari
        tarixga aralashib, modelning o'z prompti user input sifatida ko'rinishini
        oldini oladi.
        """
        query = "SELECT role, text FROM messages WHERE telegram_id = ?"
        params: list = [telegram_id]
        if max_id is not None:
            query += " AND id <= ?"
            params.append(max_id)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(query, params)
            rows = await cursor.fetchall()
        return [HistoryRow(role=r[0], text=r[1]) for r in reversed(rows)]

    async def recent_messages(self, telegram_id: int, limit: int = 10) -> list[HistoryRow]:
        """Operator handoff uchun — oxirgi N ta xabar (eskidan yangiga)."""
        return await self.recent_history(telegram_id, limit=limit)

    async def upsert_lead(
        self,
        telegram_id: int,
        *,
        contact_name: str | None = None,
        phone: str | None = None,
        business_type: str | None = None,
        store_size: str | None = None,
        notes: str | None = None,
    ) -> None:
        """Bo'sh bo'lmagan maydonlarni yangilaydi, bo'shlarni tegmaydi."""
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                INSERT INTO leads (telegram_id, contact_name, phone, business_type, store_size, notes)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET
                    contact_name  = COALESCE(excluded.contact_name,  contact_name),
                    phone         = COALESCE(excluded.phone,         phone),
                    business_type = COALESCE(excluded.business_type, business_type),
                    store_size    = COALESCE(excluded.store_size,    store_size),
                    notes         = COALESCE(excluded.notes,         notes),
                    updated_at    = CURRENT_TIMESTAMP
                """,
                (telegram_id, contact_name, phone, business_type, store_size, notes),
            )
            await db.commit()

    async def get_lead(self, telegram_id: int) -> dict | None:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM leads WHERE telegram_id = ?", (telegram_id,)
            )
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def reset_user(self, telegram_id: int) -> None:
        """/start qayta bosilganda eski suhbatni tozalash.

        Tegishli messages va lead o'chiriladi, handed_off bekor qilinadi.
        users qatori saqlanadi (telegram identifikatori o'zgarmaydi).
        """
        async with aiosqlite.connect(self._path) as db:
            await db.execute("DELETE FROM messages WHERE telegram_id = ?", (telegram_id,))
            await db.execute("DELETE FROM leads WHERE telegram_id = ?", (telegram_id,))
            await db.execute(
                "UPDATE users SET handed_off = 0, phone = NULL WHERE telegram_id = ?",
                (telegram_id,),
            )
            await db.commit()

    async def set_user_phone(self, telegram_id: int, phone: str) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "UPDATE users SET phone = ? WHERE telegram_id = ?",
                (phone, telegram_id),
            )
            await db.commit()

    async def mark_handoff(self, telegram_id: int) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "UPDATE users SET handed_off = 1 WHERE telegram_id = ?",
                (telegram_id,),
            )
            await db.commit()

    async def get_user_info(self, telegram_id: int) -> dict | None:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT telegram_id, username, first_name, last_name, phone FROM users WHERE telegram_id = ?",
                (telegram_id,),
            )
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_amocrm_lead_id(self, telegram_id: int) -> int | None:
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                "SELECT amocrm_lead_id FROM users WHERE telegram_id = ?",
                (telegram_id,),
            )
            row = await cursor.fetchone()
        return int(row[0]) if row and row[0] else None

    async def set_amocrm_lead_id(self, telegram_id: int, lead_id: int) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "UPDATE users SET amocrm_lead_id = ? WHERE telegram_id = ?",
                (lead_id, telegram_id),
            )
            await db.commit()

    async def is_handed_off(self, telegram_id: int) -> bool:
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                "SELECT handed_off FROM users WHERE telegram_id = ?",
                (telegram_id,),
            )
            row = await cursor.fetchone()
        return bool(row and row[0])
