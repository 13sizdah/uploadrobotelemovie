from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path

import aiosqlite


@dataclass(frozen=True)
class StoredFile:
    token: str
    stored_name: str
    original_name: str
    mime_type: str
    size: int
    expires_at: int


class Storage:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.files_dir = data_dir / "files"
        self.db_path = data_dir / "files.sqlite3"

    async def initialize(self) -> None:
        self.files_dir.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute(
                """CREATE TABLE IF NOT EXISTS files (
                    token TEXT PRIMARY KEY,
                    stored_name TEXT NOT NULL UNIQUE,
                    original_name TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL
                )"""
            )
            await db.execute("CREATE INDEX IF NOT EXISTS idx_files_expires ON files(expires_at)")
            await db.commit()

    async def add(self, item: StoredFile) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO files VALUES (?, ?, ?, ?, ?, ?)",
                (item.token, item.stored_name, item.original_name, item.mime_type, item.size, item.expires_at),
            )
            await db.commit()

    async def get_valid(self, token: str) -> StoredFile | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM files WHERE token = ? AND expires_at > ?", (token, int(time.time()))
            )
            row = await cursor.fetchone()
        return StoredFile(**dict(row)) if row else None

    async def delete_stored_file(self, stored_name: str) -> None:
        path = self.files_dir / stored_name
        try:
            await asyncio.to_thread(path.unlink)
        except FileNotFoundError:
            pass

    async def cleanup_expired(self) -> int:
        now = int(time.time())
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT stored_name FROM files WHERE expires_at <= ?", (now,))
            rows = await cursor.fetchall()
            await db.execute("DELETE FROM files WHERE expires_at <= ?", (now,))
            await db.commit()
        for (stored_name,) in rows:
            await self.delete_stored_file(stored_name)
        return len(rows)

    def path_for(self, stored_name: str) -> Path:
        # stored_name is generated internally and never comes from a request.
        return self.files_dir / stored_name
