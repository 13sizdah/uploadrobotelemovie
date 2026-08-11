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
    role: str | None = None,
) -> dict[str, object]:
    result = {
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
    if role:
        result["role"] = role
    return result


class ObjectStorageTests(unittest.IsolatedAsyncioTestCase):
    def test_storage_roles_control_primary_and_replication_routing(self) -> None:
        manager = ObjectStorageManager(
            (
                config("main", 1, role="primary"),
                config("iran", 2, role="replica"),
                config("archive", 3, role="download"),
                config("off", 4, role="disabled"),
            ), 8, 300,
        )

        self.assertEqual([item.name for item in manager._candidates()], ["main"])
        self.assertEqual(manager.replication_targets("main", 3, 10), ["iran"])

    async def test_explicit_replica_backend_can_be_selected_for_upload(self) -> None:
        manager = ObjectStorageManager(
            (
                config("outside", 1, role="primary"),
                config("iran", 2, role="replica"),
            ),
            8,
            300,
        )
        calls: list[str] = []

        class FakeClient:
            def __init__(self, name: str) -> None:
                self.name = name

            def upload_file(self, *args, **kwargs):
                calls.append(self.name)

        for name, backend in manager.backends.items():
            backend.client = lambda name=name: FakeClient(name)  # type: ignore[method-assign]

        selected = await manager.upload(
            Path(__file__),
            "files/selected",
            "application/octet-stream",
            preferred_backend="iran",
        )

        self.assertEqual(selected, "iran")
        self.assertEqual(calls, ["iran"])

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

    async def test_parspack_presign_omits_duplicate_content_disposition(self) -> None:
        item = config("iran", 1)
        item["endpoint_url"] = "https://bucket.parspack.net"
        manager = ObjectStorageManager((item,), 8, 300)
        captured: dict[str, object] = {}

        class FakeClient:
            def generate_presigned_url(self, operation, **kwargs):
                captured.update(kwargs["Params"])
                return "https://example.invalid/signed"

        manager.backends["iran"].client = lambda: FakeClient()  # type: ignore[method-assign]
        await manager.presigned_download("iran", "files/a", "video.mp4")

        self.assertNotIn("ResponseContentDisposition", captured)


if __name__ == "__main__":
    unittest.main()
