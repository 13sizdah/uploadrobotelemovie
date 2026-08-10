from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app.replication_api import ReplicationAPI
from app.storage import Storage, StoredFile


class ReplicationAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.temp.name))
        await self.storage.initialize()
        await self.storage.set_setting("replication_api_token", "test-worker-secret")
        item = StoredFile(
            "remote", "spool", "movie.mkv", "video/x-matroska", 500,
            int(time.time()) + 3600, "bunny", "files/remote",
        )
        await self.storage.add_with_replication_jobs(
            item, [("parspack", "files/remote")]
        )
        app = web.Application()
        ReplicationAPI(self.storage).install(app)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.headers = {"Authorization": "Bearer test-worker-secret"}

    async def asyncTearDown(self) -> None:
        await self.client.close()
        self.temp.cleanup()

    async def test_authentication_claim_renew_and_complete(self) -> None:
        rejected = await self.client.post(
            "/internal/replication/claim",
            json={"worker_id": "iran-1", "targets": ["parspack"]},
        )
        self.assertEqual(rejected.status, 401)

        claimed = await self.client.post(
            "/internal/replication/claim", headers=self.headers,
            json={"worker_id": "iran-1", "targets": ["parspack"]},
        )
        self.assertEqual(claimed.status, 200)
        job = await claimed.json()
        self.assertEqual(job["source_backend"], "bunny")
        self.assertEqual(job["target_backend"], "parspack")

        renewed = await self.client.post(
            f"/internal/replication/{job['id']}/renew", headers=self.headers,
            json={"worker_id": "iran-1"},
        )
        self.assertEqual(renewed.status, 200)
        completed = await self.client.post(
            f"/internal/replication/{job['id']}/complete", headers=self.headers,
            json={"worker_id": "iran-1"},
        )
        self.assertEqual(completed.status, 200)
        self.assertEqual(
            await self.storage.replicas_for("remote"),
            [("parspack", "files/remote")],
        )

    async def test_rotated_token_is_effective_without_restart(self) -> None:
        await self.storage.set_setting("replication_api_token", "new-secret")
        old = await self.client.post(
            "/internal/replication/claim", headers=self.headers,
            json={"worker_id": "iran-1", "targets": ["parspack"]},
        )
        self.assertEqual(old.status, 401)
        current = await self.client.post(
            "/internal/replication/claim",
            headers={"Authorization": "Bearer new-secret"},
            json={"worker_id": "iran-1", "targets": ["parspack"]},
        )
        self.assertEqual(current.status, 200)


if __name__ == "__main__":
    unittest.main()
