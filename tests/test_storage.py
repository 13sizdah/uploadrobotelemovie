from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.storage import Storage, StoredFile


class StorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.temp.name))
        await self.storage.initialize()

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    async def test_backend_reference_count_includes_primary_and_replicas(self) -> None:
        item = StoredFile("token", "stored", "movie.mkv", "video/x-matroska", 10, 4_000_000_000, "r2", "key")
        await self.storage.add_with_replicas(item, [("minio", "key")])

        self.assertEqual(await self.storage.backend_reference_count("r2"), 1)
        self.assertEqual(await self.storage.backend_reference_count("minio"), 1)
        self.assertEqual(await self.storage.backend_reference_count("unused"), 0)

    async def test_migration_switch_and_replicas_are_atomic(self) -> None:
        item = StoredFile("token", "stored", "movie.mkv", "video/x-matroska", 10, 4_000_000_000)
        await self.storage.add(item)

        await self.storage.mark_migrated_with_replicas("token", "r2", "key", [("s3", "key")])

        migrated = await self.storage.get("token")
        self.assertIsNotNone(migrated)
        self.assertEqual(migrated.backend_name, "r2")
        self.assertEqual(await self.storage.replicas_for("token"), [("s3", "key")])

    async def test_backend_usage_counts_primary_and_replica_bytes(self) -> None:
        await self.storage.add_with_replicas(
            StoredFile("a", "one", "one.bin", "application/octet-stream", 100, 4_000_000_000, "r2", "a"),
            [("minio", "a")],
        )
        await self.storage.add_with_replicas(
            StoredFile("b", "two", "two.bin", "application/octet-stream", 250, 4_000_000_000, "minio", "b"),
            [],
        )

        self.assertEqual(await self.storage.backend_usage(), {"r2": 100, "minio": 350})

    async def test_replication_jobs_survive_and_complete_transactionally(self) -> None:
        item = StoredFile(
            "queued", "spool", "movie.mkv", "video/x-matroska",
            500, 4_000_000_000, "r2", "files/queued",
        )
        await self.storage.add_with_replication_jobs(
            item, [("minio", "files/queued")]
        )

        jobs = await self.storage.due_replication_jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(await self.storage.pending_replication_count(), 1)
        self.assertEqual((await self.storage.backend_usage())["minio"], 500)

        await self.storage.complete_replication_job(jobs[0])

        self.assertEqual(await self.storage.pending_replication_count(), 0)
        self.assertEqual(
            await self.storage.replicas_for("queued"),
            [("minio", "files/queued")],
        )


if __name__ == "__main__":
    unittest.main()
