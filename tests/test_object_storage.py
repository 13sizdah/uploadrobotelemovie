from __future__ import annotations

import unittest
from unittest.mock import AsyncMock

from app.object_storage import ObjectStorageManager


def config(name: str, priority: int, enabled: bool = True) -> dict[str, object]:
    return {
        "name": name,
        "endpoint_url": "https://s3.example.com",
        "bucket": "files",
        "region": "auto",
        "access_key_id": "access",
        "secret_access_key": "secret",
        "priority": priority,
        "enabled": enabled,
    }


class ObjectStorageTests(unittest.IsolatedAsyncioTestCase):
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
