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


@dataclass(frozen=True)
class ReplicationJob:
    id: int
    token: str
    target_backend: str
    object_key: str
    attempts: int


@dataclass(frozen=True)
class ClaimedReplicationJob:
    id: int
    token: str
    target_backend: str
    object_key: str
    attempts: int
    source_backend: str
    source_object_key: str
    original_name: str
    mime_type: str
    size: int


@dataclass(frozen=True)
class AdminFile:
    token: str
    original_name: str
    size: int
    expires_at: int
    backend_name: str
    replica_count: int
    pending_count: int


@dataclass(frozen=True)
class AdminReplicationJob:
    id: int
    token: str
    original_name: str
    target_backend: str
    attempts: int
    next_attempt_at: int
    last_error: str
    claimed_by: str
    lease_until: int


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
            await db.execute(
                """CREATE TABLE IF NOT EXISTS replication_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token TEXT NOT NULL,
                    target_backend TEXT NOT NULL,
                    object_key TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    UNIQUE(token, target_backend),
                    FOREIGN KEY(token) REFERENCES files(token) ON DELETE CASCADE
                )"""
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_replication_jobs_due "
                "ON replication_jobs(next_attempt_at, id)"
            )
            job_columns = {
                row[1]
                for row in await (await db.execute("PRAGMA table_info(replication_jobs)")).fetchall()
            }
            if "claimed_by" not in job_columns:
                await db.execute(
                    "ALTER TABLE replication_jobs ADD COLUMN claimed_by TEXT NOT NULL DEFAULT ''"
                )
            if "lease_until" not in job_columns:
                await db.execute(
                    "ALTER TABLE replication_jobs ADD COLUMN lease_until INTEGER NOT NULL DEFAULT 0"
                )
            await db.execute(
                """CREATE TABLE IF NOT EXISTS replication_workers (
                    worker_id TEXT PRIMARY KEY,
                    last_seen INTEGER NOT NULL,
                    current_job INTEGER,
                    completed_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    targets TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1
                )"""
            )
            worker_columns = {
                row[1]
                for row in await (await db.execute("PRAGMA table_info(replication_workers)")).fetchall()
            }
            if "targets" not in worker_columns:
                await db.execute(
                    "ALTER TABLE replication_workers ADD COLUMN targets TEXT NOT NULL DEFAULT ''"
                )
            if "enabled" not in worker_columns:
                await db.execute(
                    "ALTER TABLE replication_workers ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1"
                )
            await db.execute(
                """CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT ''
                )"""
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at DESC)"
            )
            await db.execute(
                """CREATE TABLE IF NOT EXISTS download_daily_stats (
                    day TEXT NOT NULL,
                    backend_name TEXT NOT NULL,
                    requests INTEGER NOT NULL DEFAULT 0,
                    bytes_served INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(day, backend_name)
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

    async def update_expiry(self, token: str, expires_at: int) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "UPDATE files SET expires_at = ? WHERE token = ?",
                (expires_at, token),
            )
            await db.commit()
        return cursor.rowcount == 1

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
            await db.execute("DELETE FROM replication_jobs WHERE token = ?", (token,))
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
                    (SELECT COUNT(*) FROM file_replicas WHERE backend_name = ?) +
                    (SELECT COUNT(*) FROM replication_jobs WHERE target_backend = ?)""",
                (backend_name, backend_name, backend_name),
            )
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def backend_usage(self) -> dict[str, int]:
        now = int(time.time())
        usage: dict[str, int] = {}
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT backend_name, COALESCE(SUM(size), 0) FROM files "
                "WHERE backend_name != 'local' AND expires_at > ? GROUP BY backend_name",
                (now,),
            )
            for backend_name, size in await cursor.fetchall():
                usage[str(backend_name)] = int(size)
            cursor = await db.execute(
                """SELECT r.backend_name, COALESCE(SUM(f.size), 0)
                   FROM file_replicas r JOIN files f ON f.token = r.token
                   WHERE f.expires_at > ? GROUP BY r.backend_name""",
                (now,),
            )
            for backend_name, size in await cursor.fetchall():
                name = str(backend_name)
                usage[name] = usage.get(name, 0) + int(size)
            cursor = await db.execute(
                """SELECT j.target_backend, COALESCE(SUM(f.size), 0)
                   FROM replication_jobs j JOIN files f ON f.token = j.token
                   WHERE f.expires_at > ? GROUP BY j.target_backend""",
                (now,),
            )
            for backend_name, size in await cursor.fetchall():
                name = str(backend_name)
                usage[name] = usage.get(name, 0) + int(size)
        return usage

    async def add_with_replication_jobs(
        self,
        item: StoredFile,
        targets: list[tuple[str, str]],
    ) -> None:
        now = int(time.time())
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN")
            await db.execute(
                """INSERT INTO files
                (token, stored_name, original_name, mime_type, size, expires_at, backend_name, object_key)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    item.token, item.stored_name, item.original_name, item.mime_type,
                    item.size, item.expires_at, item.backend_name, item.object_key,
                ),
            )
            if targets:
                await db.executemany(
                    """INSERT INTO replication_jobs
                    (token, target_backend, object_key, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)""",
                    [(item.token, backend, key, now, now) for backend, key in targets],
                )
            await db.commit()

    async def due_replication_jobs(
        self, limit: int = 10, excluded_targets: tuple[str, ...] = ()
    ) -> list[ReplicationJob]:
        exclusions = ""
        params: list[object] = [int(time.time()), int(time.time()), int(time.time())]
        if excluded_targets:
            exclusions = " AND j.target_backend NOT IN (" + ",".join("?" for _ in excluded_targets) + ")"
            params.extend(excluded_targets)
        params.append(limit)
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                f"""SELECT j.id, j.token, j.target_backend, j.object_key, j.attempts
                   FROM replication_jobs j JOIN files f ON f.token = j.token
                   WHERE j.next_attempt_at <= ? AND f.expires_at > ?
                     AND (j.lease_until = 0 OR j.lease_until <= ?)
                     {exclusions}
                   ORDER BY j.next_attempt_at, j.id LIMIT ?""",
                params,
            )
            rows = await cursor.fetchall()
        return [ReplicationJob(*row) for row in rows]

    async def claim_replication_job(
        self,
        worker_id: str,
        targets: list[str],
        lease_seconds: int = 900,
    ) -> ClaimedReplicationJob | None:
        if not worker_id or not targets:
            return None
        now = int(time.time())
        placeholders = ",".join("?" for _ in targets)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                f"""SELECT j.id, j.token, j.target_backend, j.object_key, j.attempts,
                           f.backend_name, f.object_key, f.original_name, f.mime_type, f.size
                    FROM replication_jobs j JOIN files f ON f.token = j.token
                    WHERE j.next_attempt_at <= ? AND f.expires_at > ?
                      AND (j.lease_until = 0 OR j.lease_until <= ?)
                      AND j.target_backend IN ({placeholders})
                      AND f.backend_name != 'local' AND f.object_key IS NOT NULL
                    ORDER BY j.next_attempt_at, j.id LIMIT 1""",
                (now, now, now, *targets),
            )
            row = await cursor.fetchone()
            if row is None:
                await db.execute(
                    """INSERT INTO replication_workers(worker_id, last_seen, current_job, targets)
                       VALUES (?, ?, NULL, ?) ON CONFLICT(worker_id) DO UPDATE SET
                       last_seen = excluded.last_seen, current_job = NULL,
                       targets = excluded.targets""",
                    (worker_id, now, ",".join(targets)),
                )
                await db.commit()
                return None
            await db.execute(
                "UPDATE replication_jobs SET claimed_by = ?, lease_until = ?, updated_at = ? WHERE id = ?",
                (worker_id, now + max(60, lease_seconds), now, row[0]),
            )
            await db.execute(
                """INSERT INTO replication_workers(worker_id, last_seen, current_job, last_error, targets)
                   VALUES (?, ?, ?, '', ?) ON CONFLICT(worker_id) DO UPDATE SET
                   last_seen = excluded.last_seen, current_job = excluded.current_job,
                   last_error = '', targets = excluded.targets""",
                (worker_id, now, row[0], ",".join(targets)),
            )
            await db.commit()
        return ClaimedReplicationJob(*row)

    async def finish_claimed_replication_job(
        self, job_id: int, worker_id: str, error: str = ""
    ) -> str | None:
        now = int(time.time())
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """SELECT token, target_backend, object_key, attempts
                   FROM replication_jobs WHERE id = ? AND claimed_by = ?""",
                (job_id, worker_id),
            )
            row = await cursor.fetchone()
            if row is None:
                await db.rollback()
                return None
            if error:
                attempts = int(row[3]) + 1
                delay = min(3600, 15 * (2 ** min(attempts, 8)))
                await db.execute(
                    """UPDATE replication_jobs SET attempts = ?, next_attempt_at = ?,
                       last_error = ?, claimed_by = '', lease_until = 0, updated_at = ?
                       WHERE id = ?""",
                    (attempts, now + delay, error[:300], now, job_id),
                )
                await db.execute(
                    "UPDATE replication_workers SET last_seen = ?, current_job = NULL, last_error = ? WHERE worker_id = ?",
                    (now, error[:300], worker_id),
                )
            else:
                await db.execute(
                    "INSERT OR REPLACE INTO file_replicas VALUES (?, ?, ?, ?)",
                    (row[0], row[1], row[2], now),
                )
                await db.execute("DELETE FROM replication_jobs WHERE id = ?", (job_id,))
                await db.execute(
                    """UPDATE replication_workers SET last_seen = ?, current_job = NULL,
                       completed_count = completed_count + 1, last_error = '' WHERE worker_id = ?""",
                    (now, worker_id),
                )
            await db.commit()
        return str(row[0])

    async def renew_replication_lease(
        self, job_id: int, worker_id: str, lease_seconds: int = 900
    ) -> bool:
        now = int(time.time())
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """UPDATE replication_jobs SET lease_until = ?, updated_at = ?
                   WHERE id = ? AND claimed_by = ?""",
                (now + max(60, lease_seconds), now, job_id, worker_id),
            )
            await db.execute(
                "UPDATE replication_workers SET last_seen = ? WHERE worker_id = ?",
                (now, worker_id),
            )
            await db.commit()
        return cursor.rowcount == 1

    async def replication_workers(self) -> list[tuple[str, int, int | None, int, str, str, int]]:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """SELECT worker_id, last_seen, current_job, completed_count,
                          last_error, targets, enabled
                   FROM replication_workers ORDER BY last_seen DESC"""
            )
            return await cursor.fetchall()

    async def worker_is_enabled(self, worker_id: str) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT enabled FROM replication_workers WHERE worker_id = ?", (worker_id,)
            )
            row = await cursor.fetchone()
        return row is None or bool(row[0])

    async def set_worker_enabled(self, worker_id: str, enabled: bool) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "UPDATE replication_workers SET enabled = ? WHERE worker_id = ?",
                (1 if enabled else 0, worker_id),
            )
            await db.commit()
        return cursor.rowcount == 1

    async def release_worker_lease(self, worker_id: str) -> int:
        now = int(time.time())
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """UPDATE replication_jobs SET claimed_by = '', lease_until = 0,
                   next_attempt_at = 0, updated_at = ? WHERE claimed_by = ?""",
                (now, worker_id),
            )
            await db.execute(
                "UPDATE replication_workers SET current_job = NULL WHERE worker_id = ?",
                (worker_id,),
            )
            await db.commit()
        return max(0, cursor.rowcount)

    async def cancel_replication_job(self, job_id: int) -> str | None:
        now = int(time.time())
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """SELECT token FROM replication_jobs WHERE id = ?
                   AND (claimed_by = '' OR lease_until <= ?)""",
                (job_id, now),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            await db.execute("DELETE FROM replication_jobs WHERE id = ?", (job_id,))
            await db.commit()
        return str(row[0])

    async def active_worker_targets(self, max_age_seconds: int = 600) -> tuple[str, ...]:
        cutoff = int(time.time()) - max_age_seconds
        targets: set[str] = set()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT targets FROM replication_workers WHERE last_seen >= ? AND enabled = 1",
                (cutoff,),
            )
            for row in await cursor.fetchall():
                targets.update(value.strip() for value in row[0].split(",") if value.strip())
        return tuple(sorted(targets))

    async def complete_replication_job(self, job: ReplicationJob) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN")
            await db.execute(
                "INSERT OR REPLACE INTO file_replicas VALUES (?, ?, ?, ?)",
                (job.token, job.target_backend, job.object_key, int(time.time())),
            )
            await db.execute("DELETE FROM replication_jobs WHERE id = ?", (job.id,))
            await db.commit()

    async def fail_replication_job(self, job: ReplicationJob, error: str) -> None:
        attempts = job.attempts + 1
        delay = min(3600, 15 * (2 ** min(attempts, 8)))
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """UPDATE replication_jobs SET attempts = ?, next_attempt_at = ?,
                   last_error = ?, updated_at = ? WHERE id = ?""",
                (attempts, int(time.time()) + delay, error[:300], int(time.time()), job.id),
            )
            await db.commit()

    async def pending_replication_count(self, token: str | None = None) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            if token is None:
                cursor = await db.execute("SELECT COUNT(*) FROM replication_jobs")
            else:
                cursor = await db.execute(
                    "SELECT COUNT(*) FROM replication_jobs WHERE token = ?", (token,)
                )
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def admin_files(self, limit: int = 100) -> list[AdminFile]:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """SELECT f.token, f.original_name, f.size, f.expires_at,
                          f.backend_name,
                          (SELECT COUNT(*) FROM file_replicas r WHERE r.token = f.token),
                          (SELECT COUNT(*) FROM replication_jobs j WHERE j.token = f.token)
                   FROM files f ORDER BY f.expires_at DESC LIMIT ?""",
                (limit,),
            )
            rows = await cursor.fetchall()
        return [AdminFile(*row) for row in rows]

    async def admin_replication_jobs(self, limit: int = 100) -> list[AdminReplicationJob]:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """SELECT j.id, j.token, f.original_name, j.target_backend,
                          j.attempts, j.next_attempt_at, j.last_error,
                          j.claimed_by, j.lease_until
                   FROM replication_jobs j JOIN files f ON f.token = j.token
                   ORDER BY j.next_attempt_at, j.id LIMIT ?""",
                (limit,),
            )
            rows = await cursor.fetchall()
        return [AdminReplicationJob(*row) for row in rows]

    async def retry_replication_jobs(self) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "UPDATE replication_jobs SET next_attempt_at = 0, last_error = ''"
            )
            await db.commit()
        return max(0, cursor.rowcount)

    async def add_audit(self, actor: str, action: str, detail: str = "") -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO audit_log(created_at, actor, action, detail) VALUES (?, ?, ?, ?)",
                (int(time.time()), actor[:100], action[:150], detail[:500]),
            )
            await db.commit()

    async def recent_audit(self, limit: int = 200) -> list[tuple[int, str, str, str]]:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT created_at, actor, action, detail FROM audit_log "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            )
            return await cursor.fetchall()

    async def record_download(self, backend_name: str, size: int) -> None:
        day = time.strftime("%Y-%m-%d", time.gmtime())
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO download_daily_stats(day, backend_name, requests, bytes_served)
                   VALUES (?, ?, 1, ?) ON CONFLICT(day, backend_name) DO UPDATE SET
                   requests = requests + 1, bytes_served = bytes_served + excluded.bytes_served""",
                (day, backend_name[:100], max(0, size)),
            )
            await db.commit()

    async def download_statistics(
        self, days: int = 30
    ) -> list[tuple[str, str, int, int]]:
        cutoff = time.strftime(
            "%Y-%m-%d", time.gmtime(time.time() - max(1, days - 1) * 86400)
        )
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """SELECT day, backend_name, requests, bytes_served
                   FROM download_daily_stats WHERE day >= ?
                   ORDER BY day DESC, backend_name""",
                (cutoff,),
            )
            return await cursor.fetchall()

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

    async def cleanup_orphan_files(self, min_age_seconds: int = 3600) -> int:
        """Remove stale temp files that have no database record after a crash."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT stored_name FROM files")
            referenced = {str(row[0]) for row in await cursor.fetchall()}
        cutoff = time.time() - max(60, min_age_seconds)

        def cleanup() -> int:
            removed = 0
            for path in self.files_dir.iterdir():
                if not path.is_file() or path.name in referenced:
                    continue
                try:
                    if path.stat().st_mtime <= cutoff:
                        path.unlink()
                        removed += 1
                except FileNotFoundError:
                    pass
            return removed

        return await asyncio.to_thread(cleanup)

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
