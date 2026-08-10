from __future__ import annotations

import asyncio
import sqlite3
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
    backend_name: str = "local"
    object_key: str | None = None


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
            columns = {row[1] for row in await (await db.execute("PRAGMA table_info(files)")).fetchall()}
            if "backend_name" not in columns:
                await db.execute("ALTER TABLE files ADD COLUMN backend_name TEXT NOT NULL DEFAULT 'local'")
            if "object_key" not in columns:
                await db.execute("ALTER TABLE files ADD COLUMN object_key TEXT")
            await db.execute(
                """CREATE TABLE IF NOT EXISTS allowed_sources (
                    source_id INTEGER PRIMARY KEY,
                    title TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                )"""
            )
            await db.execute(
                """CREATE TABLE IF NOT EXISTS bot_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )"""
            )
            await db.execute(
                """CREATE TABLE IF NOT EXISTS file_replicas (
                    token TEXT NOT NULL,
                    backend_name TEXT NOT NULL,
                    object_key TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    PRIMARY KEY(token, backend_name),
                    FOREIGN KEY(token) REFERENCES files(token) ON DELETE CASCADE
                )"""
            )
            await db.commit()

    async def ensure_setting(self, key: str, default: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO bot_settings(key, value) VALUES (?, ?)",
                (key, default),
            )
            await db.commit()

    async def set_setting(self, key: str, value: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO bot_settings(key, value) VALUES (?, ?)",
                (key, value),
            )
            await db.commit()

    async def get_setting(self, key: str, default: str = "") -> str:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT value FROM bot_settings WHERE key = ?", (key,))
            row = await cursor.fetchone()
        return row[0] if row else default

    async def add_source(self, source_id: int, title: str, source_type: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO allowed_sources VALUES (?, ?, ?, ?)",
                (source_id, title, source_type, int(time.time())),
            )
            await db.commit()

    async def remove_source(self, source_id: int) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM allowed_sources WHERE source_id = ?", (source_id,))
            await db.commit()

    async def source_is_allowed(self, source_id: int) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT 1 FROM allowed_sources WHERE source_id = ?", (source_id,)
            )
            return await cursor.fetchone() is not None

    async def list_sources(self) -> list[tuple[int, str, str]]:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT source_id, title, source_type FROM allowed_sources ORDER BY created_at DESC"
            )
            return await cursor.fetchall()

    async def statistics(self) -> tuple[int, int, int, int]:
        now = int(time.time())
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """SELECT
                    COUNT(*),
                    COALESCE(SUM(CASE WHEN expires_at > ? THEN 1 ELSE 0 END), 0),
                    COALESCE(SUM(CASE WHEN expires_at <= ? THEN 1 ELSE 0 END), 0),
                    COALESCE(SUM(CASE WHEN expires_at > ? THEN size ELSE 0 END), 0)
                FROM files""",
                (now, now, now),
            )
            row = await cursor.fetchone()
        assert row is not None
        return int(row[0]), int(row[1]), int(row[2]), int(row[3])

    async def recent_valid_files(self, limit: int = 8) -> list[StoredFile]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM files WHERE expires_at > ? ORDER BY expires_at DESC LIMIT ?",
                (int(time.time()), limit),
            )
            rows = await cursor.fetchall()
        return [StoredFile(**dict(row)) for row in rows]

    async def delete_by_token(self, token: str) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT stored_name FROM files WHERE token = ?", (token,))
            row = await cursor.fetchone()
            if row is None:
                return False
            await db.execute("DELETE FROM files WHERE token = ?", (token,))
            await db.commit()
        await self.delete_stored_file(row[0])
        return True

    async def get(self, token: str) -> StoredFile | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM files WHERE token = ?", (token,))
            row = await cursor.fetchone()
        return StoredFile(**dict(row)) if row else None

    async def expired_files(self) -> list[StoredFile]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM files WHERE expires_at <= ?", (int(time.time()),))
            rows = await cursor.fetchall()
        return [StoredFile(**dict(row)) for row in rows]

    async def delete_record(self, token: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM file_replicas WHERE token = ?", (token,))
            await db.execute("DELETE FROM files WHERE token = ?", (token,))
            await db.commit()

    async def add_replicas(self, token: str, replicas: list[tuple[str, str]]) -> None:
        if not replicas:
            return
        async with aiosqlite.connect(self.db_path) as db:
            await db.executemany(
                "INSERT OR REPLACE INTO file_replicas VALUES (?, ?, ?, ?)",
                [(token, backend, key, int(time.time())) for backend, key in replicas],
            )
            await db.commit()

    async def replicas_for(self, token: str) -> list[tuple[str, str]]:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT backend_name, object_key FROM file_replicas WHERE token = ?",
                (token,),
            )
            return await cursor.fetchall()

    async def backend_reference_count(self, backend_name: str) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """SELECT
                    (SELECT COUNT(*) FROM files WHERE backend_name = ?) +
                    (SELECT COUNT(*) FROM file_replicas WHERE backend_name = ?)""",
                (backend_name, backend_name),
            )
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def local_files(self, limit: int = 1000) -> list[StoredFile]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM files WHERE backend_name = 'local' AND expires_at > ? LIMIT ?",
                (int(time.time()), limit),
            )
            rows = await cursor.fetchall()
        return [StoredFile(**dict(row)) for row in rows]

    async def mark_migrated(self, token: str, backend_name: str, object_key: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE files SET backend_name = ?, object_key = ? WHERE token = ?",
                (backend_name, object_key, token),
            )
            await db.commit()

    async def mark_migrated_with_replicas(
        self,
        token: str,
        backend_name: str,
        object_key: str,
        replicas: list[tuple[str, str]],
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN")
            cursor = await db.execute(
                "UPDATE files SET backend_name = ?, object_key = ? "
                "WHERE token = ? AND backend_name = 'local'",
                (backend_name, object_key, token),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                raise RuntimeError("File is no longer eligible for migration")
            if replicas:
                await db.executemany(
                    "INSERT OR REPLACE INTO file_replicas VALUES (?, ?, ?, ?)",
                    [(token, backend, key, int(time.time())) for backend, key in replicas],
                )
            await db.commit()

    async def create_database_backup(self, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)

        def backup() -> None:
            with sqlite3.connect(self.db_path) as source, sqlite3.connect(destination) as target:
                source.backup(target)

        await asyncio.to_thread(backup)

    async def add(self, item: StoredFile) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO files
                (token, stored_name, original_name, mime_type, size, expires_at, backend_name, object_key)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    item.token, item.stored_name, item.original_name, item.mime_type,
                    item.size, item.expires_at, item.backend_name, item.object_key,
                ),
            )
            await db.commit()

    async def add_with_replicas(
        self, item: StoredFile, replicas: list[tuple[str, str]]
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO files
                (token, stored_name, original_name, mime_type, size, expires_at, backend_name, object_key)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    item.token, item.stored_name, item.original_name, item.mime_type,
                    item.size, item.expires_at, item.backend_name, item.object_key,
                ),
            )
            if replicas:
                await db.executemany(
                    "INSERT INTO file_replicas VALUES (?, ?, ?, ?)",
                    [(item.token, backend, key, int(time.time())) for backend, key in replicas],
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
