from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app.admin_web import AdminWeb, Session
from app.object_storage import ObjectStorageManager
from app.secure_config import EncryptedConfigStore, hash_password
from app.storage import Storage, StoredFile


class AdminWebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        data_dir = Path(self.temp.name)
        self.storage = Storage(data_dir)
        await self.storage.initialize()
        self.manager = ObjectStorageManager((), 8, 300)
        self.admin = AdminWeb(
            hash_password("a-secure-test-password"),
            self.manager,
            EncryptedConfigStore(data_dir),
            self.storage,
        )
        app = web.Application()
        self.admin.install(app)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.admin.sessions["test-session"] = Session(
            time.monotonic() + 600, "csrf-token"
        )
        self.headers = {"Cookie": "admin_session=test-session"}

    async def asyncTearDown(self) -> None:
        await self.client.close()
        self.temp.cleanup()

    async def test_dashboard_and_all_sections_render(self) -> None:
        expected = {
            "/manage/": "وضعیت سرویس",
            "/manage/files": "<h2>فایل‌ها</h2>",
            "/manage/storage": "مسیر فایل‌های جدید",
            "/manage/jobs": "صف انتقال پایدار",
            "/manage/system": "وضعیت backendها",
        }
        for path, marker in expected.items():
            response = await self.client.get(path, headers=self.headers)
            self.assertEqual(response.status, 200, path)
            self.assertIn(marker, await response.text())

    async def test_file_list_escapes_untrusted_filename(self) -> None:
        await self.storage.add(
            StoredFile(
                "token", "stored", "<script>alert(1)</script>.mkv",
                "video/x-matroska", 10, 4_000_000_000,
            )
        )

        response = await self.client.get("/manage/files", headers=self.headers)
        body = await response.text()

        self.assertNotIn("<script>alert", body)
        self.assertIn("&lt;script&gt;", body)


if __name__ == "__main__":
    unittest.main()
