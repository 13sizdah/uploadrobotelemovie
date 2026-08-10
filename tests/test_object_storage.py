from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from app.object_storage import ObjectStorageManager, UploadCancelled


def config(
    name: str,
    priority: int,
    enabled: bool = True,
    capacity_bytes: int = 0,
    reserve_bytes: int = 0,
) -> dict[str, object]:
    return {
        "name": name,
        "endpoint_url": "https://s3.example.com",
        "bucket": "files",
        "region": "auto",
        "access_key_id": "access",
        "secret_access_key": "secret",
        "priority": priority,
        "enabled": enabled,
        "capacity_bytes": capacity_bytes,
        "reserve_bytes": reserve_bytes,
    }


class ObjectStorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_user_cancellation_does_not_mark_backend_unhealthy(self) -> None:
        manager = ObjectStorageManager((config("primary", 1),), 8, 300)
        backend = manager.backends["primary"]

        class FakeClient:
            def upload_file(self, *args, **kwargs):
                kwargs["Callback"](1024)

        backend.client = lambda: FakeClient()  # type: ignore[method-assign]
        with self.assertRaises(UploadCancelled):
            await manager.upload(
                Path(__file__), "files/test", "application/octet-stream",
                cancelled=lambda: True,
            )
        self.assertEqual(backend.failures, 0)

    def test_capacity_routing_skips_backend_without_room(self) -> None:
        manager = ObjectStorageManager(
            (
                config("almost-full", 1, capacity_bytes=1000, reserve_bytes=100),
                config("available", 20, capacity_bytes=5000, reserve_bytes=100),
            ),
            8,
            300,
        )

        candidates = manager._candidates(200, {"almost-full": 800, "available": 1000})

        self.assertEqual([item.name for item in candidates], ["available"])

    def test_capacity_routing_balances_by_utilization(self) -> None:
        manager = ObjectStorageManager(
            (
                config("busy", 1, capacity_bytes=10_000),
                config("quiet", 100, capacity_bytes=10_000),
            ),
            8,
            300,
        )

        candidates = manager._candidates(100, {"busy": 8000, "quiet": 1000})

        self.assertEqual([item.name for item in candidates], ["quiet", "busy"])

    async def test_resolve_download_falls_back_when_primary_object_is_missing(self) -> None:
        manager = ObjectStorageManager((config("primary", 1), config("replica", 2)), 8, 300)
        manager.object_exists = AsyncMock(side_effect=[False, True])

        selected = await manager.resolve_download_location(
            [("primary", "files/a"), ("replica", "files/a")]
        )

        self.assertEqual(selected, ("replica", "files/a"))

    async def test_enabled_backend_is_checked_before_disabled_backend(self) -> None:
        manager = ObjectStorageManager(
            (config("disabled", 1, False), config("enabled", 20, True)), 8, 300
        )
        manager.object_exists = AsyncMock(return_value=True)

        selected = await manager.resolve_download_location(
            [("disabled", "files/a"), ("enabled", "files/a")]
        )

        self.assertEqual(selected, ("enabled", "files/a"))


if __name__ == "__main__":
    unittest.main()
