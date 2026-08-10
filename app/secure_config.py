from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path

from cryptography.fernet import Fernet


def hash_password(password: str, iterations: int = 600_000) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return "pbkdf2_sha256${}${}${}".format(
        iterations,
        base64.urlsafe_b64encode(salt).decode(),
        base64.urlsafe_b64encode(digest).decode(),
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, raw_iterations, raw_salt, raw_digest = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(raw_salt)
        expected = base64.urlsafe_b64decode(raw_digest)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(raw_iterations))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


class EncryptedConfigStore:
    def __init__(self, data_dir: Path) -> None:
        self.key_path = data_dir / "config.key"
        self.config_path = data_dir / "s3-backends.enc"

    def _fernet(self) -> Fernet:
        if not self.key_path.exists():
            self.key_path.parent.mkdir(parents=True, exist_ok=True)
            self.key_path.write_bytes(Fernet.generate_key())
            self.key_path.chmod(0o600)
        return Fernet(self.key_path.read_bytes())

    async def load(self) -> list[dict[str, object]] | None:
        if not self.config_path.exists():
            return None

        def read() -> list[dict[str, object]]:
            decoded = self._fernet().decrypt(self.config_path.read_bytes())
            value = json.loads(decoded)
            if not isinstance(value, list):
                raise ValueError("Encrypted S3 config is not a list")
            return value

        return await asyncio.to_thread(read)

    async def save(self, configs: list[dict[str, object]]) -> None:
        def write() -> None:
            payload = json.dumps(configs, separators=(",", ":")).encode()
            temporary = self.config_path.with_suffix(".tmp")
            temporary.write_bytes(self._fernet().encrypt(payload))
            temporary.chmod(0o600)
            temporary.replace(self.config_path)

        await asyncio.to_thread(write)
