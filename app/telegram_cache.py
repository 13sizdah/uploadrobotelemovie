from __future__ import annotations

from pathlib import Path

CACHE_ROOT = Path("/var/lib/telegram-bot-api")


def validated_cache_path(file_path: str, local_api_enabled: bool) -> Path | None:
    if not local_api_enabled:
        return None
    candidate = Path(file_path)
    if not candidate.is_absolute():
        return None
    try:
        candidate.resolve().relative_to(CACHE_ROOT)
    except (OSError, ValueError):
        return None
    return candidate
