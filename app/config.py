from __future__ import annotations

import os
import json
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
    admin_user_id: int
    forward_only: bool
    telegram_api_base: str | None
    telegram_file_base: str | None
    storage_backend: str
    s3_backends: tuple[dict[str, object], ...]
    s3_presigned_url_seconds: int
    s3_multipart_chunk_mb: int
    admin_web_password_hash: str | None

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
        admin_user_id = _non_negative_int("ADMIN_USER_ID", 0)
        forward_only = os.getenv("FORWARD_ONLY", "false").strip().lower() in {"1", "true", "yes", "on"}
        if forward_only and not admin_user_id:
            raise ValueError("ADMIN_USER_ID is required when FORWARD_ONLY is enabled")
        storage_backend = os.getenv("STORAGE_BACKEND", "local").strip().lower()
        if storage_backend not in {"local", "s3"}:
            raise ValueError("STORAGE_BACKEND must be local or s3")
        raw_s3 = os.getenv("S3_BACKENDS_JSON", "[]").strip() or "[]"
        try:
            s3_backends = json.loads(raw_s3)
        except json.JSONDecodeError as exc:
            raise ValueError("S3_BACKENDS_JSON is not valid JSON") from exc
        if not isinstance(s3_backends, list):
            raise ValueError("S3_BACKENDS_JSON must be a JSON array")
        required = {"name", "endpoint_url", "bucket", "access_key_id", "secret_access_key"}
        for backend in s3_backends:
            if not isinstance(backend, dict) or not required.issubset(backend):
                raise ValueError("Every S3 backend must include name, endpoint_url, bucket and credentials")
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
            admin_user_id=admin_user_id,
            forward_only=forward_only,
            telegram_api_base=os.getenv("TELEGRAM_API_BASE") or None,
            telegram_file_base=os.getenv("TELEGRAM_FILE_BASE") or None,
            storage_backend=storage_backend,
            s3_backends=tuple(s3_backends),
            s3_presigned_url_seconds=_positive_int("S3_PRESIGNED_URL_SECONDS", 300),
            s3_multipart_chunk_mb=_positive_int("S3_MULTIPART_CHUNK_MB", 64),
            admin_web_password_hash=os.getenv("ADMIN_WEB_PASSWORD_HASH") or None,
        )
