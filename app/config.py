from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _positive_int(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


def _non_negative_int(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value < 0:
        raise ValueError(f"{name} must be zero or greater")
    return value


@dataclass(frozen=True)
class Settings:
    bot_token: str
    public_base_url: str
    file_ttl_hours: int
    max_file_size_mb: int
    host: str
    port: int
    data_dir: Path
    cleanup_interval_seconds: int
    allowed_user_ids: frozenset[int]
    telegram_api_base: str | None
    telegram_file_base: str | None

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        token = os.getenv("BOT_TOKEN", "").strip()
        base_url = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
        if not token:
            raise ValueError("BOT_TOKEN is required")
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("PUBLIC_BASE_URL must start with http:// or https://")

        raw_ids = os.getenv("ALLOWED_USER_IDS", "").strip()
        allowed_ids = frozenset(int(item.strip()) for item in raw_ids.split(",") if item.strip())
        return cls(
            bot_token=token,
            public_base_url=base_url,
            file_ttl_hours=_positive_int("FILE_TTL_HOURS", 24),
            max_file_size_mb=_non_negative_int("MAX_FILE_SIZE_MB", 0),
            host=os.getenv("HOST", "0.0.0.0"),
            port=_positive_int("PORT", 8080),
            data_dir=Path(os.getenv("DATA_DIR", "./data")).resolve(),
            cleanup_interval_seconds=_positive_int("CLEANUP_INTERVAL_SECONDS", 300),
            allowed_user_ids=allowed_ids,
            telegram_api_base=os.getenv("TELEGRAM_API_BASE") or None,
            telegram_file_base=os.getenv("TELEGRAM_FILE_BASE") or None,
        )
