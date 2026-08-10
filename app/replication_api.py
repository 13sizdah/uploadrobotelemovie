from __future__ import annotations

import hmac
from aiohttp import web

from .storage import Storage


class ReplicationAPI:
    """Authenticated controller API used by remote replication workers."""

    def __init__(self, storage: Storage) -> None:
        self.storage = storage

    def install(self, app: web.Application) -> None:
        app.router.add_post("/internal/replication/claim", self.claim)
        app.router.add_post("/internal/replication/{job_id}/complete", self.complete)
        app.router.add_post("/internal/replication/{job_id}/fail", self.fail)
        app.router.add_post("/internal/replication/{job_id}/renew", self.renew)

    async def authorize(self, request: web.Request) -> None:
        supplied = request.headers.get("Authorization", "").removeprefix("Bearer ")
        expected = await self.storage.get_setting("replication_api_token")
        if not supplied or not expected or not hmac.compare_digest(supplied, expected):
            raise web.HTTPUnauthorized(
                text="Unauthorized", headers={"WWW-Authenticate": "Bearer"}
            )

    async def claim(self, request: web.Request) -> web.Response:
        await self.authorize(request)
        data = await request.json()
        worker_id = str(data.get("worker_id", ""))[:100]
        targets = [str(value)[:100] for value in data.get("targets", []) if value]
        if not worker_id or not targets or len(targets) > 20:
            raise web.HTTPBadRequest(text="worker_id and targets are required")
        job = await self.storage.claim_replication_job(worker_id, targets)
        if job is None:
            return web.Response(status=204)
        return web.json_response(
            {
                "id": job.id,
                "token": job.token,
                "source_backend": job.source_backend,
                "source_object_key": job.source_object_key,
                "target_backend": job.target_backend,
                "target_object_key": job.object_key,
                "original_name": job.original_name,
                "mime_type": job.mime_type,
                "size": job.size,
                "attempts": job.attempts,
            }
        )

    async def complete(self, request: web.Request) -> web.Response:
        await self.authorize(request)
        data = await request.json()
        worker_id = str(data.get("worker_id", ""))[:100]
        job_id = int(request.match_info["job_id"])
        token = await self.storage.finish_claimed_replication_job(job_id, worker_id)
        if token is None:
            raise web.HTTPConflict(text="Job lease is no longer valid")
        if await self.storage.pending_replication_count(token) == 0:
            item = await self.storage.get(token)
            if item is not None:
                await self.storage.delete_stored_file(item.stored_name)
        return web.json_response({"ok": True})

    async def fail(self, request: web.Request) -> web.Response:
        await self.authorize(request)
        data = await request.json()
        worker_id = str(data.get("worker_id", ""))[:100]
        error = str(data.get("error", "WorkerError"))[:300]
        job_id = int(request.match_info["job_id"])
        if await self.storage.finish_claimed_replication_job(job_id, worker_id, error) is None:
            raise web.HTTPConflict(text="Job lease is no longer valid")
        return web.json_response({"ok": True})

    async def renew(self, request: web.Request) -> web.Response:
        await self.authorize(request)
        data = await request.json()
        worker_id = str(data.get("worker_id", ""))[:100]
        job_id = int(request.match_info["job_id"])
        if not await self.storage.renew_replication_lease(job_id, worker_id):
            raise web.HTTPConflict(text="Job lease is no longer valid")
        return web.json_response({"ok": True})
