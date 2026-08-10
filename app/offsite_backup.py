from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet

from .object_storage import ObjectStorageManager
from .secure_config import EncryptedConfigStore
from .storage import Storage


async def create_offsite_backup(
    storage: Storage,
    config_store: EncryptedConfigStore,
    manager: ObjectStorageManager,
    backend_name: str,
) -> str:
    """Create and upload a consistent metadata/config archive to a private S3 bucket."""
    backup_dir = storage.data_dir / "backups"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    snapshot = backup_dir / f"offsite-{stamp}.sqlite3"
    archive = backup_dir / f"offsite-{stamp}.tar.gz"
    await storage.create_database_backup(snapshot)

    def build_archive() -> None:
        with tarfile.open(archive, "w:gz") as output:
            output.add(snapshot, arcname="data/files.sqlite3")
            if config_store.key_path.is_file():
                output.add(config_store.key_path, arcname="data/config.key")
            if config_store.config_path.is_file():
                output.add(config_store.config_path, arcname="data/s3-backends.enc")
        archive.chmod(0o600)

    try:
        await asyncio.to_thread(build_archive)
        digest = await asyncio.to_thread(
            lambda: hashlib.sha256(archive.read_bytes()).hexdigest()
        )
        object_key = f"system-backups/{archive.name}"
        await manager.upload_to(
            backend_name, archive, object_key, "application/gzip"
        )
        await manager.prune_prefix(backend_name, "system-backups/", keep=7)
        await storage.set_setting("last_offsite_backup_at", str(int(datetime.now().timestamp())))
        await storage.set_setting("last_offsite_backup_status", f"ok:{backend_name}:{digest}")
        return object_key
    except Exception as exc:
        await storage.set_setting(
            "last_offsite_backup_status", f"error:{type(exc).__name__}"
        )
        raise
    finally:
        await asyncio.to_thread(snapshot.unlink, missing_ok=True)
        await asyncio.to_thread(archive.unlink, missing_ok=True)


async def verify_latest_offsite_backup(
    storage: Storage,
    manager: ObjectStorageManager,
    backend_name: str,
) -> dict[str, object]:
    """Download and validate the newest archive without changing live data."""
    object_key = await manager.latest_object_key(backend_name, "system-backups/")
    if not object_key:
        raise RuntimeError("No offsite backup exists")
    with tempfile.TemporaryDirectory(dir=storage.data_dir) as directory:
        temporary = Path(directory)
        archive_path = temporary / "backup.tar.gz"
        database_path = temporary / "files.sqlite3"
        await manager.download_to(backend_name, object_key, archive_path)

        def verify() -> dict[str, object]:
            digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
            with tarfile.open(archive_path, "r:gz") as archive:
                names = set(archive.getnames())
                required = {
                    "data/files.sqlite3", "data/config.key", "data/s3-backends.enc"
                }
                if not required.issubset(names):
                    raise RuntimeError("Backup archive is missing recovery files")
                database = archive.extractfile("data/files.sqlite3")
                key_file = archive.extractfile("data/config.key")
                encrypted_file = archive.extractfile("data/s3-backends.enc")
                if database is None or key_file is None or encrypted_file is None:
                    raise RuntimeError("Backup archive members are unreadable")
                database_path.write_bytes(database.read())
                decoded = Fernet(key_file.read()).decrypt(encrypted_file.read())
                configs = json.loads(decoded)
                if not isinstance(configs, list):
                    raise RuntimeError("S3 configuration is invalid")
            with sqlite3.connect(database_path) as db:
                integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
                file_count = int(db.execute("SELECT COUNT(*) FROM files").fetchone()[0])
            if integrity != "ok":
                raise RuntimeError(f"SQLite integrity check failed: {integrity}")
            return {
                "object_key": object_key,
                "sha256": digest,
                "file_count": file_count,
                "backend_count": len(configs),
            }

        try:
            result = await asyncio.to_thread(verify)
            await storage.set_setting(
                "last_restore_test_status",
                f"ok:{backend_name}:{object_key}:{result['file_count']}",
            )
            return result
        except Exception as exc:
            await storage.set_setting(
                "last_restore_test_status", f"error:{type(exc).__name__}"
            )
            raise
