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
            "/manage/storage/routing": "<h2>مسیر فایل‌های جدید</h2>",
            "/manage/storage/new": "<h2>افزودن فضای جدید</h2>",
            "/manage/jobs": "صف انتقال پایدار",
            "/manage/system": "وضعیت backendها",
            "/manage/audit": "رویدادهای مدیریتی",
            "/manage/settings": "تغییر رمز پنل",
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

    async def test_file_expiry_can_be_extended_or_made_permanent(self) -> None:
        original_expiry = int(time.time()) + 3600
        await self.storage.add(
            StoredFile(
                "expiry-token", "stored", "movie.mkv", "video/x-matroska",
                10, original_expiry,
            )
        )
        response = await self.client.post(
            "/manage/files/expiry", headers=self.headers,
            data={"csrf": "csrf-token", "token": "expiry-token", "action": "24"},
            allow_redirects=False,
        )
        self.assertEqual(response.status, 302)
        self.assertEqual(
            (await self.storage.get("expiry-token")).expires_at,
            original_expiry + 24 * 3600,
        )
        response = await self.client.post(
            "/manage/files/expiry", headers=self.headers,
            data={"csrf": "csrf-token", "token": "expiry-token", "action": "permanent"},
            allow_redirects=False,
        )
        self.assertEqual(response.status, 302)
        self.assertEqual((await self.storage.get("expiry-token")).expires_at, 4_102_444_800)

    async def test_operational_alert_settings_are_validated_and_saved(self) -> None:
        response = await self.client.post(
            "/manage/settings/operations", headers=self.headers,
            data={
                "csrf": "csrf-token", "disk_threshold": "85",
                "queue_threshold": "25", "backup_backend": "",
            },
            allow_redirects=False,
        )
        self.assertEqual(response.status, 302)
        self.assertEqual(await self.storage.get_setting("alert_disk_percent"), "85")
        self.assertEqual(await self.storage.get_setting("alert_queue_count"), "25")

        rejected = await self.client.post(
            "/manage/settings/operations", headers=self.headers,
            data={
                "csrf": "csrf-token", "disk_threshold": "20",
                "queue_threshold": "0", "backup_backend": "",
            },
            allow_redirects=False,
        )
        self.assertEqual(rejected.status, 400)

    async def test_storage_sections_are_split_into_separate_pages(self) -> None:
        overview = await self.client.get("/manage/storage", headers=self.headers)
        overview_body = await overview.text()
        self.assertNotIn("<h2>مسیر فایل‌های جدید</h2>", overview_body)
        self.assertNotIn("<h2>افزودن فضای جدید</h2>", overview_body)

        routing = await self.client.get("/manage/storage/routing", headers=self.headers)
        self.assertIn("<h2>مسیر فایل‌های جدید</h2>", await routing.text())
        adding = await self.client.get("/manage/storage/new", headers=self.headers)
        self.assertIn("<h2>افزودن فضای جدید</h2>", await adding.text())

    async def test_backup_and_password_change_are_persistent(self) -> None:
        response = await self.client.post(
            "/manage/backups/create",
            headers=self.headers,
            data={"csrf": "csrf-token"},
            allow_redirects=False,
        )
        self.assertEqual(response.status, 302)
        self.assertEqual(len(list((self.storage.data_dir / "backups").glob("admin-*.sqlite3"))), 1)

        response = await self.client.post(
            "/manage/settings/password",
            headers=self.headers,
            data={
                "csrf": "csrf-token",
                "current_password": "a-secure-test-password",
                "new_password": "a-new-secure-password",
                "confirm_password": "a-new-secure-password",
            },
            allow_redirects=False,
        )
        self.assertEqual(response.status, 302)
        encoded = await self.storage.get_setting("admin_web_password_hash")
        self.assertTrue(encoded.startswith("pbkdf2_sha256$"))
        self.assertGreaterEqual(len(await self.storage.recent_audit()), 3)


if __name__ == "__main__":
    unittest.main()
