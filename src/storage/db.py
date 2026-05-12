from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    telegram_id   INTEGER PRIMARY KEY,
    username      TEXT,
    first_name    TEXT,
    last_name     TEXT,
    phone         TEXT,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
            await db.commit()

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

    async def recent_history(self, telegram_id: int, limit: int = 20) -> list[HistoryRow]:
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                """
                SELECT role, text FROM messages
                WHERE telegram_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (telegram_id, limit),
            )
            rows = await cursor.fetchall()
        return [HistoryRow(role=r[0], text=r[1]) for r in reversed(rows)]
