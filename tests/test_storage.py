from __future__ import annotations

import tempfile
import os
import time
import unittest
from pathlib import Path

from app.storage import Storage, StoredFile


class StorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_orphan_cleanup_preserves_database_files(self) -> None:
        referenced = StoredFile(
            "kept-token", "kept-file", "kept.bin", "application/octet-stream",
            10, int(time.time()) + 3600,
        )
        await self.storage.add(referenced)
        kept = self.storage.path_for("kept-file")
        orphan = self.storage.path_for("orphan-file")
        kept.write_bytes(b"kept")
        orphan.write_bytes(b"orphan")
        old = time.time() - 7200
        os.utime(kept, (old, old))
        os.utime(orphan, (old, old))

        removed = await self.storage.cleanup_orphan_files(3600)

        self.assertEqual(removed, 1)
        self.assertTrue(kept.exists())
        self.assertFalse(orphan.exists())

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

    async def test_remote_worker_claim_is_exclusive_and_completes(self) -> None:
        item = StoredFile(
            "remote", "spool", "movie.mkv", "video/x-matroska",
            500, 4_000_000_000, "bunny", "files/remote",
        )
        await self.storage.add_with_replication_jobs(
            item, [("parspack", "files/remote")]
        )

        claimed = await self.storage.claim_replication_job(
            "iran-1", ["parspack"], lease_seconds=900
        )
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.source_backend, "bunny")
        self.assertIsNone(
            await self.storage.claim_replication_job("iran-2", ["parspack"])
        )
        self.assertEqual(await self.storage.due_replication_jobs(), [])
        self.assertTrue(await self.storage.renew_replication_lease(claimed.id, "iran-1"))
        self.assertTrue(
            await self.storage.finish_claimed_replication_job(claimed.id, "iran-1")
        )
        self.assertEqual(
            await self.storage.replicas_for("remote"),
            [("parspack", "files/remote")],
        )
        workers = await self.storage.replication_workers()
        self.assertEqual(workers[0][0], "iran-1")
        self.assertEqual(workers[0][3], 1)

    async def test_local_worker_can_exclude_remote_targets(self) -> None:
        item = StoredFile(
            "routing", "spool", "movie.mkv", "video/x-matroska",
            500, 4_000_000_000, "bunny", "files/routing",
        )
        await self.storage.add_with_replication_jobs(
            item, [("parspack", "files/routing")]
        )
        self.assertEqual(
            await self.storage.due_replication_jobs(excluded_targets=("parspack",)),
            [],
        )

    async def test_download_statistics_are_aggregated_per_day_and_backend(self) -> None:
        await self.storage.record_download("bunny", 100)
        await self.storage.record_download("bunny", 250)
        await self.storage.record_download("parspack", 50)

        stats = await self.storage.download_statistics(30)
        by_backend = {backend: (requests, size) for _, backend, requests, size in stats}
        self.assertEqual(by_backend["bunny"], (2, 350))
        self.assertEqual(by_backend["parspack"], (1, 50))

    async def test_worker_can_be_disabled_and_lease_released(self) -> None:
        item = StoredFile(
            "worker-control", "spool", "movie.mkv", "video/x-matroska",
            500, 4_000_000_000, "bunny", "files/worker-control",
        )
        await self.storage.add_with_replication_jobs(
            item, [("parspack", "files/worker-control")]
        )
        job = await self.storage.claim_replication_job("iran-1", ["parspack"])
        self.assertIsNotNone(job)
        self.assertTrue(await self.storage.worker_is_enabled("iran-1"))
        self.assertTrue(await self.storage.set_worker_enabled("iran-1", False))
        self.assertFalse(await self.storage.worker_is_enabled("iran-1"))
        self.assertEqual(await self.storage.active_worker_targets(), ())
        self.assertEqual(await self.storage.release_worker_lease("iran-1"), 1)
        self.assertEqual(len(await self.storage.due_replication_jobs()), 1)

    async def test_active_replication_cannot_be_cancelled(self) -> None:
        item = StoredFile(
            "cancel-control", "spool", "movie.mkv", "video/x-matroska",
            500, 4_000_000_000, "bunny", "files/cancel-control",
        )
        await self.storage.add_with_replication_jobs(
            item, [("parspack", "files/cancel-control")]
        )
        pending = (await self.storage.due_replication_jobs())[0]
        self.assertEqual(
            await self.storage.cancel_replication_job(pending.id), "cancel-control"
        )

        await self.storage.add_with_replication_jobs(
            StoredFile(
                "active-cancel", "spool-2", "movie2.mkv", "video/x-matroska",
                500, 4_000_000_000, "bunny", "files/active-cancel",
            ),
            [("parspack", "files/active-cancel")],
        )
        claimed = await self.storage.claim_replication_job("iran-2", ["parspack"])
        self.assertIsNotNone(claimed)
        self.assertIsNone(await self.storage.cancel_replication_job(claimed.id))


if __name__ == "__main__":
    unittest.main()
