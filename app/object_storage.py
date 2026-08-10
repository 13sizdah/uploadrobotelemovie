from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config

logger = logging.getLogger(__name__)


@dataclass
class S3Backend:
    name: str
    endpoint_url: str
    bucket: str
    region: str
    access_key_id: str
    secret_access_key: str
    priority: int = 100
    enabled: bool = True
    failures: int = 0
    unhealthy_until: float = 0
    latency_ms: float = 0

    def client(self):
        return boto3.client(
            "s3",
            endpoint_url=self.endpoint_url,
            region_name=self.region,
            aws_access_key_id=self.access_key_id,
            aws_secret_access_key=self.secret_access_key,
            config=Config(signature_version="s3v4", retries={"max_attempts": 4, "mode": "adaptive"}),
        )


class ObjectStorageManager:
    def __init__(self, configs: tuple[dict[str, object], ...], chunk_mb: int, presign_seconds: int):
        self.backends = {
            str(item["name"]): S3Backend(
                name=str(item["name"]),
                endpoint_url=str(item["endpoint_url"]).rstrip("/"),
                bucket=str(item["bucket"]),
                region=str(item.get("region", "auto")),
                access_key_id=str(item["access_key_id"]),
                secret_access_key=str(item["secret_access_key"]),
                priority=int(item.get("priority", 100)),
                enabled=bool(item.get("enabled", True)),
            )
            for item in configs
        }
        chunk = chunk_mb * 1024 * 1024
        self.transfer_config = TransferConfig(
            multipart_threshold=chunk,
            multipart_chunksize=chunk,
            max_concurrency=4,
            use_threads=True,
        )
        self.presign_seconds = presign_seconds

    def export_configs(self) -> list[dict[str, object]]:
        return [
            {
                "name": item.name, "endpoint_url": item.endpoint_url, "bucket": item.bucket,
                "region": item.region, "access_key_id": item.access_key_id,
                "secret_access_key": item.secret_access_key, "priority": item.priority,
                "enabled": item.enabled,
            }
            for item in self.backends.values()
        ]

    def replace_configs(self, configs: list[dict[str, object]]) -> None:
        replacement = ObjectStorageManager(tuple(configs), self.transfer_config.multipart_chunksize // (1024 * 1024), self.presign_seconds)
        self.backends = replacement.backends

    def _candidates(self) -> list[S3Backend]:
        now = time.monotonic()
        candidates = [b for b in self.backends.values() if b.enabled and b.unhealthy_until <= now]
        return sorted(candidates, key=lambda b: (b.failures * 1000 + b.priority, b.latency_ms))

    async def health_check(self, backend: S3Backend) -> bool:
        started = time.monotonic()
        try:
            await asyncio.to_thread(backend.client().head_bucket, Bucket=backend.bucket)
            backend.latency_ms = (time.monotonic() - started) * 1000
            backend.failures = 0
            backend.unhealthy_until = 0
            return True
        except Exception as exc:
            backend.failures += 1
            backend.unhealthy_until = time.monotonic() + min(300, 15 * 2 ** backend.failures)
            logger.warning("S3 health check failed for %s: %s", backend.name, type(exc).__name__)
            return False

    async def health_check_all(self) -> dict[str, bool]:
        results = await asyncio.gather(
            *(self.health_check(backend) for backend in self.backends.values()),
            return_exceptions=True,
        )
        return {
            name: result is True
            for name, result in zip(self.backends, results)
        }

    async def upload(
        self,
        source: Path,
        object_key: str,
        content_type: str,
        progress: Callable[[int], None] | None = None,
    ) -> str:
        errors: list[str] = []
        for backend in self._candidates():
            transferred = 0
            lock = threading.Lock()

            def callback(amount: int) -> None:
                nonlocal transferred
                with lock:
                    transferred += amount
                    if progress:
                        progress(transferred)

            try:
                await asyncio.to_thread(
                    backend.client().upload_file,
                    str(source), backend.bucket, object_key,
                    ExtraArgs={"ContentType": content_type},
                    Callback=callback,
                    Config=self.transfer_config,
                )
                backend.failures = 0
                return backend.name
            except Exception as exc:
                backend.failures += 1
                backend.unhealthy_until = time.monotonic() + min(300, 15 * 2 ** backend.failures)
                errors.append(f"{backend.name}:{type(exc).__name__}")
                logger.exception("S3 upload failed on backend %s", backend.name)
        raise RuntimeError("All S3 backends failed: " + ", ".join(errors))

    async def upload_to(
        self,
        backend_name: str,
        source: Path,
        object_key: str,
        content_type: str,
    ) -> None:
        backend = self.backends[backend_name]
        await asyncio.to_thread(
            backend.client().upload_file,
            str(source), backend.bucket, object_key,
            ExtraArgs={"ContentType": content_type},
            Config=self.transfer_config,
        )

    async def replicate(
        self,
        source: Path,
        object_key: str,
        content_type: str,
        primary_backend: str,
        desired_total: int,
    ) -> list[tuple[str, str]]:
        candidates = [item for item in self._candidates() if item.name != primary_backend]
        replicas: list[tuple[str, str]] = []
        for backend in candidates[: max(0, desired_total - 1)]:
            try:
                await self.upload_to(backend.name, source, object_key, content_type)
                replicas.append((backend.name, object_key))
                backend.failures = 0
            except Exception:
                backend.failures += 1
                backend.unhealthy_until = time.monotonic() + min(300, 15 * 2 ** backend.failures)
                logger.exception("Replication failed on backend %s", backend.name)
        return replicas

    def best_location(self, locations: list[tuple[str, str]]) -> tuple[str, str] | None:
        available = []
        now = time.monotonic()
        for backend_name, object_key in locations:
            backend = self.backends.get(backend_name)
            if backend and backend.enabled and backend.unhealthy_until <= now:
                available.append((backend.failures * 1000 + backend.priority, backend.latency_ms, backend_name, object_key))
        if not available:
            return locations[0] if locations else None
        _, _, backend_name, object_key = min(available)
        return backend_name, object_key

    async def object_exists(self, backend_name: str, object_key: str) -> bool:
        backend = self.backends.get(backend_name)
        if backend is None:
            return False
        started = time.monotonic()
        try:
            await asyncio.to_thread(
                backend.client().head_object,
                Bucket=backend.bucket,
                Key=object_key,
            )
            backend.latency_ms = (time.monotonic() - started) * 1000
            backend.failures = 0
            backend.unhealthy_until = 0
            return True
        except Exception as exc:
            backend.failures += 1
            backend.unhealthy_until = time.monotonic() + min(300, 15 * 2 ** backend.failures)
            logger.warning(
                "S3 object probe failed for %s/%s: %s",
                backend.name,
                object_key,
                type(exc).__name__,
            )
            return False

    async def resolve_download_location(
        self, locations: list[tuple[str, str]]
    ) -> tuple[str, str] | None:
        unique_locations = list(dict.fromkeys(locations))
        now = time.monotonic()

        def rank(location: tuple[str, str]) -> tuple[int, float, float]:
            backend = self.backends.get(location[0])
            if backend is None:
                return (3, float("inf"), float("inf"))
            availability = 0 if backend.enabled and backend.unhealthy_until <= now else 1
            return (availability, backend.failures * 1000 + backend.priority, backend.latency_ms)

        for backend_name, object_key in sorted(unique_locations, key=rank):
            if await self.object_exists(backend_name, object_key):
                return backend_name, object_key
        return None

    async def delete(self, backend_name: str, object_key: str) -> None:
        backend = self.backends.get(backend_name)
        if backend is None:
            raise RuntimeError(f"Unknown S3 backend: {backend_name}")
        await asyncio.to_thread(
            backend.client().delete_object,
            Bucket=backend.bucket,
            Key=object_key,
        )

    async def presigned_download(self, backend_name: str, object_key: str, filename: str) -> str:
        backend = self.backends.get(backend_name)
        if backend is None:
            raise RuntimeError(f"Unknown S3 backend: {backend_name}")
        return await asyncio.to_thread(
            backend.client().generate_presigned_url,
            "get_object",
            Params={
                "Bucket": backend.bucket,
                "Key": object_key,
                "ResponseContentDisposition": f'attachment; filename="{filename}"',
            },
            ExpiresIn=self.presign_seconds,
        )
