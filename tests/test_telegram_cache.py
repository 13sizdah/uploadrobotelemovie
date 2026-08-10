from __future__ import annotations

import unittest

from app.telegram_cache import validated_cache_path


class TelegramCacheTests(unittest.TestCase):
    def test_accepts_only_absolute_paths_inside_dedicated_cache(self) -> None:
        accepted = validated_cache_path(
            "/var/lib/telegram-bot-api/8815/documents/movie.mkv", True
        )
        self.assertIsNotNone(accepted)
        self.assertIsNone(validated_cache_path("documents/movie.mkv", True))
        self.assertIsNone(validated_cache_path("/etc/passwd", True))
        self.assertIsNone(
            validated_cache_path(
                "/var/lib/telegram-bot-api/8815/../../../../etc/passwd", True
            )
        )
        self.assertIsNone(
            validated_cache_path(
                "/var/lib/telegram-bot-api/8815/documents/movie.mkv", False
            )
        )


if __name__ == "__main__":
    unittest.main()
