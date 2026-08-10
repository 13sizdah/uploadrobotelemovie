from __future__ import annotations

import asyncio
import html
import logging
import secrets
import time
from dataclasses import dataclass

from aiohttp import web

from .object_storage import ObjectStorageManager
from .secure_config import EncryptedConfigStore, verify_password
from .storage import Storage

logger = logging.getLogger(__name__)


@dataclass
class Session:
    expires_at: float
    csrf: str


class AdminWeb:
    def __init__(self, password_hash: str, manager: ObjectStorageManager, store: EncryptedConfigStore, storage: Storage):
        self.password_hash = password_hash
        self.manager = manager
        self.store = store
        self.storage = storage
        self.sessions: dict[str, Session] = {}
        self.failed_logins: dict[str, list[float]] = {}
        self.migration_task: asyncio.Task[None] | None = None
        self.migration_status = "آماده برای انتقال فایل‌های محلی"

    def install(self, app: web.Application) -> None:
        app.router.add_get("/manage", self.redirect_to_index)
        app.router.add_get("/manage/", self.index)
        app.router.add_post("/manage/login", self.login)
        app.router.add_post("/manage/logout", self.logout)
        app.router.add_post("/manage/storage/add", self.add_storage)
        app.router.add_post("/manage/storage/mode", self.set_storage_mode)
        app.router.add_post("/manage/storage/replication", self.set_replication)
        app.router.add_post("/manage/storage/migrate", self.start_migration)
        app.router.add_post("/manage/storage/toggle", self.toggle_storage)
        app.router.add_post("/manage/storage/priority", self.set_storage_priority)
        app.router.add_post("/manage/storage/delete", self.delete_storage)

    async def redirect_to_index(self, _: web.Request) -> web.Response:
        raise web.HTTPPermanentRedirect("/manage/")

    def session(self, request: web.Request) -> Session | None:
        token = request.cookies.get("admin_session", "")
        session = self.sessions.get(token)
        if session and session.expires_at > time.monotonic():
            session.expires_at = time.monotonic() + 1800
            return session
        self.sessions.pop(token, None)
        return None

    def page(self, body: str) -> web.Response:
        markup = f"""<!doctype html><html lang="fa" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow"><title>مدیریت ذخیره‌سازی</title><style>
        *{{box-sizing:border-box}}body{{margin:0;background:#07152b;color:#f7faff;font:16px/1.7 Tahoma;padding:24px}}main{{max-width:760px;margin:auto}}.card{{background:#0d203a;border:1px solid #29405e;border-radius:20px;padding:24px;margin:16px 0}}h1,h2{{margin-top:0}}label{{display:block;margin-top:13px;color:#bdcbe0}}input,select{{width:100%;min-height:46px;border:1px solid #405775;border-radius:10px;background:#071426;color:white;padding:10px;direction:ltr}}button{{min-height:46px;border:0;border-radius:10px;background:#43d6c5;color:#06211e;font-weight:bold;padding:10px 18px;margin-top:18px;cursor:pointer}}button.danger{{background:#ff9d9d;color:#3d0909}}button:focus-visible,input:focus-visible{{outline:3px solid #8cf7ea;outline-offset:2px}}.row{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}.muted{{color:#aebed5}}.ok{{color:#70e7b8}}.bad{{color:#ff9d9d}}a{{color:#8cf7ea}}@media(max-width:600px){{.row{{grid-template-columns:1fr}}}}
        </style></head><body><main><h1>پنل امن ذخیره‌سازی</h1>{body}</main></body></html>"""
        response = web.Response(text=markup, content_type="text/html", charset="utf-8")
        response.headers.update({
            "Cache-Control": "no-store", "X-Frame-Options": "DENY",
            "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'",
        })
        return response

    async def index(self, request: web.Request) -> web.Response:
        session = self.session(request)
        if not session:
            return self.page('<section class="card"><h2>ورود مدیر</h2><form method="post" action="/manage/login"><label for="password">رمز عبور</label><input id="password" name="password" type="password" autocomplete="current-password" required><button>ورود امن</button></form></section>')
        mode = await self.storage.get_setting("storage_backend", "local")
        replication_count = await self.storage.get_setting("replication_count", "1")
        cards_parts: list[str] = []
        for item in self.manager.backends.values():
            name = html.escape(item.name)
            refs = await self.storage.backend_reference_count(item.name)
            healthy = item.unhealthy_until <= time.monotonic()
            state_class = "ok" if item.enabled and healthy else "bad"
            state = "فعال و سالم" if item.enabled and healthy else ("غیرفعال برای آپلود" if not item.enabled else "موقتاً ناسالم")
            cards_parts.append(
                f'''<section class="card"><h2>{name}</h2><p>Bucket: <b>{html.escape(item.bucket)}</b></p><p>Endpoint: {html.escape(item.endpoint_url)}</p><p>Region: {html.escape(item.region)}</p><p>Latency: {item.latency_ms:.0f} ms | خطا: {item.failures} | ارجاع فایل: {refs}</p><p class="{state_class}">{state}</p><div class="row"><form method="post" action="/manage/storage/priority"><input type="hidden" name="csrf" value="{session.csrf}"><input type="hidden" name="name" value="{name}"><label>اولویت</label><input name="priority" type="number" value="{item.priority}" min="1" max="9999"><button>ذخیره اولویت</button></form><form method="post" action="/manage/storage/toggle"><input type="hidden" name="csrf" value="{session.csrf}"><input type="hidden" name="name" value="{name}"><button>{'غیرفعال‌کردن آپلود' if item.enabled else 'فعال‌کردن آپلود'}</button></form></div><form method="post" action="/manage/storage/delete"><input type="hidden" name="csrf" value="{session.csrf}"><input type="hidden" name="name" value="{name}"><button class="danger">حذف تنظیمات این فضا</button></form></section>'''
            )
        cards = "".join(cards_parts) or '<p class="muted">هنوز فضای S3 ثبت نشده است.</p>'
        migration_status = html.escape(self.migration_status)
        return self.page(f'''<section class="card"><h2>مسیر فایل‌های جدید</h2><p>حالت فعلی: <b>{html.escape(mode)}</b></p><form method="post" action="/manage/storage/mode"><input type="hidden" name="csrf" value="{session.csrf}"><select name="mode"><option value="s3">S3 هوشمند</option><option value="local">دیسک محلی</option></select><button>اعمال حالت</button></form><form method="post" action="/manage/storage/replication"><input type="hidden" name="csrf" value="{session.csrf}"><label>تعداد کل نسخه‌های هر فایل</label><input name="count" type="number" value="{html.escape(replication_count)}" min="1" max="5"><button>ذخیره تعداد نسخه‌ها</button></form></section>''' + cards + f'''<section class="card"><h2>انتقال فایل‌های قدیمی</h2><p class="muted">فایل‌های فعال روی دیسک، پس از آپلود موفق و ثبت در دیتابیس به S3 منتقل می‌شوند. نسخه محلی فقط در پایان هر انتقال پاک می‌شود.</p><p>{migration_status}</p><form method="post" action="/manage/storage/migrate"><input type="hidden" name="csrf" value="{session.csrf}"><button>شروع انتقال امن به S3</button></form></section><section class="card"><h2>افزودن فضای جدید</h2><form method="post" action="/manage/storage/add"><input type="hidden" name="csrf" value="{session.csrf}"><div class="row"><div><label>نام یکتا</label><input name="name" required></div><div><label>Region</label><input name="region" value="auto" required></div></div><label>Endpoint HTTPS</label><input name="endpoint_url" type="url" required><label>Bucket</label><input name="bucket" required><div class="row"><div><label>Access Key</label><input name="access_key_id" required></div><div><label>Secret Key</label><input name="secret_access_key" type="password" required></div></div><label>اولویت (عدد کمتر بهتر)</label><input name="priority" type="number" value="100" min="1" max="9999"><button>تست اتصال و ذخیره</button></form></section><form method="post" action="/manage/logout"><input type="hidden" name="csrf" value="{session.csrf}"><button>خروج</button></form>''')

    async def login(self, request: web.Request) -> web.Response:
        ip = request.remote or "unknown"
        now = time.monotonic()
        attempts = [value for value in self.failed_logins.get(ip, []) if now - value < 900]
        if len(attempts) >= 5:
            raise web.HTTPTooManyRequests(text="Too many login attempts")
        data = await request.post()
        if not verify_password(str(data.get("password", "")), self.password_hash):
            attempts.append(now)
            self.failed_logins[ip] = attempts
            return self.page('<section class="card"><p class="bad">رمز عبور نادرست است.</p><a href="/manage/">بازگشت</a></section>')
        self.failed_logins.pop(ip, None)
        token = secrets.token_urlsafe(32)
        self.sessions[token] = Session(now + 1800, secrets.token_urlsafe(24))
        response = web.HTTPFound("/manage/")
        response.set_cookie("admin_session", token, max_age=1800, httponly=True, secure=True, samesite="Strict", path="/manage")
        raise response

    async def require_form_session(self, request: web.Request) -> tuple[Session, dict[str, str]]:
        session = self.session(request)
        if not session:
            raise web.HTTPForbidden(text="Authentication required")
        data = {key: str(value).strip() for key, value in (await request.post()).items()}
        if not secrets.compare_digest(data.get("csrf", ""), session.csrf):
            raise web.HTTPForbidden(text="Invalid CSRF token")
        return session, data

    async def logout(self, request: web.Request) -> web.Response:
        await self.require_form_session(request)
        token = request.cookies.get("admin_session", "")
        self.sessions.pop(token, None)
        response = web.HTTPFound("/manage/")
        response.del_cookie("admin_session", path="/manage")
        raise response

    async def add_storage(self, request: web.Request) -> web.Response:
        _, data = await self.require_form_session(request)
        endpoint = data.get("endpoint_url", "")
        if not endpoint.startswith("https://"):
            return self.page('<section class="card"><p class="bad">Endpoint باید HTTPS باشد.</p></section>')
        if data.get("name") in self.manager.backends:
            return self.page('<section class="card"><p class="bad">این نام قبلاً ثبت شده است.</p></section>')
        try:
            priority = int(data.get("priority", "100"))
        except ValueError:
            return self.page('<section class="card"><p class="bad">اولویت معتبر نیست.</p></section>')
        config: dict[str, object] = {
            "name": data.get("name", ""), "endpoint_url": endpoint,
            "bucket": data.get("bucket", ""), "region": data.get("region", "auto"),
            "access_key_id": data.get("access_key_id", ""),
            "secret_access_key": data.get("secret_access_key", ""),
            "priority": priority, "enabled": True,
        }
        probe = ObjectStorageManager((config,), self.manager.transfer_config.multipart_chunksize // (1024 * 1024), self.manager.presign_seconds)
        backend = next(iter(probe.backends.values()))
        if not await probe.health_check(backend):
            return self.page('<section class="card"><p class="bad">اتصال یا دسترسی Bucket ناموفق بود؛ چیزی ذخیره نشد.</p></section>')
        configs = self.manager.export_configs() + [config]
        await self.store.save(configs)
        self.manager.replace_configs(configs)
        await self.storage.set_setting("storage_backend", "s3")
        raise web.HTTPFound("/manage/")

    async def set_storage_mode(self, request: web.Request) -> web.Response:
        _, data = await self.require_form_session(request)
        mode = data.get("mode", "")
        if mode not in {"local", "s3"}:
            raise web.HTTPBadRequest(text="Invalid storage mode")
        if mode == "s3" and not self.manager.backends:
            return self.page('<section class="card"><p class="bad">ابتدا یک فضای S3 سالم اضافه کنید.</p></section>')
        await self.storage.set_setting("storage_backend", mode)
        raise web.HTTPFound("/manage/")

    async def set_replication(self, request: web.Request) -> web.Response:
        _, data = await self.require_form_session(request)
        try:
            count = int(data.get("count", "1"))
        except ValueError:
            raise web.HTTPBadRequest(text="Invalid replication count")
        if not 1 <= count <= 5:
            raise web.HTTPBadRequest(text="Replication count must be between 1 and 5")
        await self.storage.set_setting("replication_count", str(count))
        raise web.HTTPFound("/manage/")

    async def _save_backend_configs(self, configs: list[dict[str, object]]) -> None:
        await self.store.save(configs)
        self.manager.replace_configs(configs)

    async def toggle_storage(self, request: web.Request) -> web.Response:
        _, data = await self.require_form_session(request)
        name = data.get("name", "")
        if name not in self.manager.backends:
            raise web.HTTPNotFound(text="Storage backend not found")
        configs = self.manager.export_configs()
        for config in configs:
            if config["name"] == name:
                config["enabled"] = not bool(config.get("enabled", True))
        await self._save_backend_configs(configs)
        if not any(item.enabled for item in self.manager.backends.values()):
            await self.storage.set_setting("storage_backend", "local")
        raise web.HTTPFound("/manage/")

    async def set_storage_priority(self, request: web.Request) -> web.Response:
        _, data = await self.require_form_session(request)
        name = data.get("name", "")
        if name not in self.manager.backends:
            raise web.HTTPNotFound(text="Storage backend not found")
        try:
            priority = int(data.get("priority", "100"))
        except ValueError:
            raise web.HTTPBadRequest(text="Invalid priority")
        if not 1 <= priority <= 9999:
            raise web.HTTPBadRequest(text="Priority must be between 1 and 9999")
        configs = self.manager.export_configs()
        for config in configs:
            if config["name"] == name:
                config["priority"] = priority
        await self._save_backend_configs(configs)
        raise web.HTTPFound("/manage/")

    async def delete_storage(self, request: web.Request) -> web.Response:
        _, data = await self.require_form_session(request)
        name = data.get("name", "")
        if name not in self.manager.backends:
            raise web.HTTPNotFound(text="Storage backend not found")
        references = await self.storage.backend_reference_count(name)
        if references:
            return self.page(
                f'<section class="card"><p class="bad">این فضا هنوز {references} ارجاع فایل دارد و برای جلوگیری از خرابی لینک‌ها حذف نشد.</p><a href="/manage/">بازگشت</a></section>'
            )
        configs = [item for item in self.manager.export_configs() if item["name"] != name]
        await self._save_backend_configs(configs)
        if not any(item.enabled for item in self.manager.backends.values()):
            await self.storage.set_setting("storage_backend", "local")
        raise web.HTTPFound("/manage/")

    async def start_migration(self, request: web.Request) -> web.Response:
        await self.require_form_session(request)
        if not self.manager.backends:
            return self.page('<section class="card"><p class="bad">ابتدا حداقل یک فضای S3 اضافه کنید.</p></section>')
        if self.migration_task and not self.migration_task.done():
            raise web.HTTPFound("/manage/")
        self.migration_status = "در حال آماده‌سازی فهرست فایل‌ها…"
        self.migration_task = asyncio.create_task(self._migrate_local_files())
        raise web.HTTPFound("/manage/")

    async def _migrate_local_files(self) -> None:
        migrated = 0
        failed = 0
        try:
            files = await self.storage.local_files()
            total = len(files)
            desired_total = max(1, int(await self.storage.get_setting("replication_count", "1")))
            if total == 0:
                self.migration_status = "فایل محلی فعالی برای انتقال وجود ندارد."
                return
            for position, item in enumerate(files, start=1):
                source = self.storage.path_for(item.stored_name)
                if not source.is_file():
                    failed += 1
                    self.migration_status = f"فایل {position} از {total}: نسخه محلی پیدا نشد"
                    continue
                object_key = f"migrated/{item.token}/{item.stored_name}"
                uploaded: list[tuple[str, str]] = []
                registered = False
                try:
                    self.migration_status = f"در حال انتقال فایل {position} از {total}…"
                    primary = await self.manager.upload(source, object_key, item.mime_type)
                    uploaded.append((primary, object_key))
                    replicas = await self.manager.replicate(
                        source, object_key, item.mime_type, primary, desired_total
                    )
                    uploaded.extend(replicas)
                    await self.storage.mark_migrated_with_replicas(
                        item.token, primary, object_key, replicas
                    )
                    registered = True
                    try:
                        await self.storage.delete_stored_file(item.stored_name)
                    except Exception:
                        logger.exception("Remote migration succeeded but local cleanup failed for %s", item.token)
                    migrated += 1
                except Exception:
                    failed += 1
                    logger.exception("Local-to-S3 migration failed for token %s", item.token)
                    if not registered:
                        for backend_name, remote_key in uploaded:
                            try:
                                await self.manager.delete(backend_name, remote_key)
                            except Exception:
                                logger.exception("Could not remove failed migration object from %s", backend_name)
            self.migration_status = f"انتقال پایان یافت: {migrated} موفق، {failed} ناموفق از {total} فایل"
        except Exception:
            logger.exception("Local-to-S3 migration task failed")
            self.migration_status = "انتقال به علت خطای داخلی متوقف شد؛ فایل‌های محلی حفظ شده‌اند."
