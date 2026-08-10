from __future__ import annotations

import asyncio
import hashlib
import tarfile
from datetime import datetime, timezone
from pathlib import Path

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
