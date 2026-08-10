from __future__ import annotations

import tarfile
import tempfile
import time
import unittest
from pathlib import Path

from app.offsite_backup import create_offsite_backup, verify_latest_offsite_backup
from app.secure_config import EncryptedConfigStore
from app.storage import Storage, StoredFile


class FakeManager:
    def __init__(self) -> None:
        self.upload: tuple[str, str, str] | None = None
        self.members: set[str] = set()
        self.pruned: tuple[str, str, int] | None = None
        self.archive = b""
        self.object_key = ""

    async def upload_to(
        self, backend: str, source: Path, object_key: str, content_type: str
    ) -> None:
        self.upload = backend, object_key, content_type
        self.object_key = object_key
        self.archive = source.read_bytes()
        with tarfile.open(source, "r:gz") as archive:
            self.members = set(archive.getnames())

    async def prune_prefix(self, backend: str, prefix: str, keep: int) -> int:
        self.pruned = backend, prefix, keep
        return 0

    async def latest_object_key(self, backend: str, prefix: str) -> str | None:
        return self.object_key or None

    async def download_to(self, backend: str, object_key: str, destination: Path) -> None:
        destination.write_bytes(self.archive)


class OffsiteBackupTests(unittest.IsolatedAsyncioTestCase):
    async def test_backup_contains_database_and_encrypted_s3_recovery_pair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            storage = Storage(data_dir)
            await storage.initialize()
            await storage.add(
                StoredFile(
                    "token", "stored", "file.bin", "application/octet-stream",
                    10, int(time.time()) + 3600,
                )
            )
            config = EncryptedConfigStore(data_dir)
            await config.save([{
                "name": "backup", "endpoint_url": "https://s3.example.com",
                "bucket": "bucket", "access_key_id": "id",
                "secret_access_key": "secret",
            }])
            manager = FakeManager()

            key = await create_offsite_backup(storage, config, manager, "backup")

            self.assertTrue(key.startswith("system-backups/offsite-"))
            self.assertEqual(manager.upload[0], "backup")
            self.assertEqual(manager.pruned, ("backup", "system-backups/", 7))
            self.assertEqual(
                manager.members,
                {"data/files.sqlite3", "data/config.key", "data/s3-backends.enc"},
            )
            self.assertTrue(
                (await storage.get_setting("last_offsite_backup_status")).startswith("ok:backup:")
            )
            self.assertEqual(list((data_dir / "backups").glob("offsite-*")), [])

            result = await verify_latest_offsite_backup(storage, manager, "backup")
            self.assertEqual(result["file_count"], 1)
            self.assertEqual(result["backend_count"], 1)
            self.assertEqual(result["object_key"], key)
            self.assertTrue(
                (await storage.get_setting("last_restore_test_status")).startswith(
                    "ok:backup:system-backups/"
                )
            )


if __name__ == "__main__":
    unittest.main()
