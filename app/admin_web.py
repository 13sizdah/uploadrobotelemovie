from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import shutil
import secrets
import time
from dataclasses import dataclass
from datetime import datetime

from aiohttp import web

from .object_storage import ObjectStorageManager
from .secure_config import EncryptedConfigStore, hash_password, verify_password
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
        self.started_at = time.monotonic()

    def install(self, app: web.Application) -> None:
        app.router.add_get("/manage", self.redirect_to_index)
        app.router.add_get("/manage/", self.index)
        app.router.add_get("/manage/files", self.files_page)
        app.router.add_get("/manage/storage", self.storage_page)
        app.router.add_get("/manage/storage/routing", self.storage_routing_page)
        app.router.add_get("/manage/storage/new", self.storage_add_page)
        app.router.add_get("/manage/jobs", self.jobs_page)
        app.router.add_get("/manage/system", self.system_page)
        app.router.add_get("/manage/audit", self.audit_page)
        app.router.add_get("/manage/settings", self.settings_page)
        app.router.add_get("/manage/backups/{name}", self.download_backup)
        app.router.add_get("/manage/jobs/pause", self.redirect_to_jobs)
        for path in (
            "/manage/storage/add",
            "/manage/storage/mode",
            "/manage/storage/replication",
            "/manage/storage/migrate",
            "/manage/storage/toggle",
            "/manage/storage/role",
            "/manage/storage/priority",
            "/manage/storage/capacity",
            "/manage/storage/delete",
        ):
            app.router.add_get(path, self.redirect_to_storage)
        app.router.add_post("/manage/login", self.login)
        app.router.add_post("/manage/logout", self.logout)
        app.router.add_post("/manage/files/delete", self.delete_file)
        app.router.add_post("/manage/jobs/retry", self.retry_jobs)
        app.router.add_post("/manage/jobs/pause", self.toggle_replication_pause)
        app.router.add_post("/manage/settings/password", self.change_password)
        app.router.add_post("/manage/backups/create", self.create_backup)
        app.router.add_post("/manage/storage/add", self.add_storage)
        app.router.add_post("/manage/storage/mode", self.set_storage_mode)
        app.router.add_post("/manage/storage/replication", self.set_replication)
        app.router.add_post("/manage/storage/migrate", self.start_migration)
        app.router.add_post("/manage/storage/toggle", self.toggle_storage)
        app.router.add_post("/manage/storage/role", self.set_storage_role)
        app.router.add_post("/manage/storage/priority", self.set_storage_priority)
        app.router.add_post("/manage/storage/capacity", self.set_storage_capacity)
        app.router.add_post("/manage/storage/delete", self.delete_storage)

    async def redirect_to_index(self, _: web.Request) -> web.Response:
        raise web.HTTPPermanentRedirect("/manage/")

    async def redirect_to_storage(self, _: web.Request) -> web.Response:
        """Recover cleanly when a POST-only action URL is refreshed or bookmarked."""
        raise web.HTTPSeeOther("/manage/storage")

    async def redirect_to_jobs(self, _: web.Request) -> web.Response:
        raise web.HTTPSeeOther("/manage/jobs")

    def session(self, request: web.Request) -> Session | None:
        token = request.cookies.get("admin_session", "")
        session = self.sessions.get(token)
        if session and session.expires_at > time.monotonic():
            session.expires_at = time.monotonic() + 1800
            return session
        self.sessions.pop(token, None)
        return None

    def page(self, body: str, active: str = "", authenticated: bool = False) -> web.Response:
        if not authenticated and "مسیر فایل‌های جدید" in body:
            active, authenticated = "storage", True
        links = (("dashboard", "/manage/", "◈", "داشبورد"), ("files", "/manage/files", "▤", "فایل‌ها"), ("storage", "/manage/storage", "◉", "فضاها"), ("jobs", "/manage/jobs", "⇄", "صف انتقال"), ("system", "/manage/system", "◌", "سیستم"), ("audit", "/manage/audit", "≡", "رویدادها"), ("settings", "/manage/settings", "⚙", "تنظیمات"))
        navigation = ""
        if authenticated:
            navigation = '<aside><div class="brand"><span class="brand-mark">↑</span><div><b>FileFlow</b><small>مدیریت ربات</small></div></div><nav aria-label="منوی مدیریت">' + "".join(
                f'<a class="{"active" if key == active else ""}" href="{url}"><i>{icon}</i><span>{label}</span></a>'
                for key, url, icon, label in links
            ) + '</nav><div class="side-status"><span></span><div><b>سرویس فعال</b><small>اتصال امن برقرار است</small></div></div></aside>'
        body_class = "app-body" if authenticated else "login-body"
        header = '<header class="topbar"><div><p>مرکز کنترل</p><h1>مدیریت فایل و فضای ابری</h1></div><span class="live"><i></i>آنلاین</span></header>' if authenticated else ''
        layout = f'{navigation}<div class="content">{header}<main>{body}</main></div>' if authenticated else f'<main class="login-shell">{body}</main>'
        markup = f"""<!doctype html><html lang="fa" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow"><meta name="theme-color" content="#07130f"><title>داشبورد ربات فایل</title><style>
        :root{{--bg:#07130f;--surface:#0d1d17;--surface-2:#11251d;--line:#234137;--text:#f5fbf8;--muted:#94aea3;--brand:#58e2ae;--brand-2:#20ba85;--danger:#ff8f91;--warn:#f7c56b;--shadow:0 22px 65px #02080666}}*{{box-sizing:border-box}}html{{color-scheme:dark}}body{{margin:0;min-height:100vh;background:var(--bg);color:var(--text);font:15px/1.8 Tahoma,"Segoe UI",sans-serif}}body:before{{content:"";position:fixed;inset:0;pointer-events:none;background:radial-gradient(circle at 80% 0,#154b3866,transparent 32%),radial-gradient(circle at 5% 95%,#103a4b44,transparent 28%)}}a{{color:#85efd0;text-decoration:none}}.app-body{{display:grid;grid-template-columns:250px minmax(0,1fr);direction:rtl}}aside{{position:sticky;top:0;height:100vh;padding:24px 17px;border-left:1px solid var(--line);background:#091711e8;backdrop-filter:blur(18px);z-index:3}}.brand{{display:flex;align-items:center;gap:12px;padding:4px 10px 26px;border-bottom:1px solid var(--line)}}.brand-mark{{display:grid;place-items:center;width:42px;height:42px;border-radius:13px;background:linear-gradient(145deg,var(--brand),var(--brand-2));color:#052018;font-size:24px;font-weight:bold;box-shadow:0 10px 28px #36d9a43d}}.brand b,.brand small{{display:block}}.brand b{{font-size:18px;letter-spacing:.3px}}.brand small{{color:var(--muted);font-size:11px}}nav{{display:flex;flex-direction:column;gap:5px;margin-top:24px}}nav a{{display:flex;align-items:center;gap:12px;padding:11px 13px;color:var(--muted);border:1px solid transparent;border-radius:12px;transition:.18s ease}}nav a i{{display:grid;place-items:center;width:27px;height:27px;font-style:normal;font-size:18px}}nav a:hover{{color:white;background:#10281f}}nav a.active{{color:white;background:linear-gradient(100deg,#173c2e,#10291f);border-color:#2c5b49;box-shadow:inset -3px 0 var(--brand)}}.side-status{{position:absolute;bottom:22px;right:17px;left:17px;display:flex;align-items:center;gap:10px;padding:12px;background:#0d2119;border:1px solid var(--line);border-radius:13px}}.side-status>span,.live i{{width:9px;height:9px;border-radius:50%;background:var(--brand);box-shadow:0 0 0 5px #58e2ae1c}}.side-status b,.side-status small{{display:block}}.side-status b{{font-size:12px}}.side-status small{{color:var(--muted);font-size:10px}}.content{{min-width:0;padding:30px 34px 50px}}.topbar{{display:flex;align-items:center;justify-content:space-between;gap:20px;max-width:1320px;margin:0 auto 25px}}.topbar p{{margin:0;color:var(--brand);font-size:12px}}.topbar h1{{margin:2px 0 0;font-size:25px;line-height:1.4}}.live{{display:flex;align-items:center;gap:9px;padding:7px 12px;border:1px solid #2c5848;background:#10271e;border-radius:99px;font-size:12px}}main{{max-width:1320px;margin:auto}}.grid{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:15px}}.two{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:15px}}.card{{position:relative;background:linear-gradient(145deg,#10251d,#0b1b15);border:1px solid var(--line);border-radius:18px;padding:21px;margin:0 0 15px;box-shadow:var(--shadow);overflow:hidden}}.card:after{{content:"";position:absolute;inset:0;pointer-events:none;background:linear-gradient(120deg,#ffffff05,transparent 28%)}}.metric{{min-height:134px}}.metric:before{{content:"";position:absolute;width:75px;height:75px;left:-20px;top:-22px;border-radius:50%;background:#58e2ae10}}.metric b{{display:block;margin-top:16px;font-size:28px;line-height:1.2;color:white;direction:ltr;text-align:right}}.metric span,.muted{{color:var(--muted)}}h2,h3{{margin:0 0 12px;line-height:1.45}}h2{{font-size:18px}}p{{margin:7px 0}}label{{display:block;margin:13px 0 6px;color:#b9d0c7;font-size:13px}}input,select{{width:100%;min-height:46px;border:1px solid #315347;border-radius:11px;background:#07150f;color:white;padding:10px 12px;direction:ltr;transition:.18s}}input:hover,select:hover{{border-color:#477361}}input:focus,select:focus{{border-color:var(--brand);box-shadow:0 0 0 3px #58e2ae1a;outline:0}}button{{position:relative;min-height:43px;border:0;border-radius:11px;background:linear-gradient(120deg,var(--brand),#39ce99);color:#052019;font-weight:bold;padding:9px 17px;margin-top:14px;cursor:pointer;transition:.18s;z-index:1}}button:hover{{transform:translateY(-1px);filter:brightness(1.06)}}button.danger{{background:#55282c;color:#ffc6c7;border:1px solid #844148}}button.secondary{{background:#19362a;color:#d9eee6;border:1px solid #315646}}button:focus-visible,a:focus-visible{{outline:3px solid #9ff5da;outline-offset:3px}}.row{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:13px}}.ok{{color:#6ce5b6}}.bad{{color:var(--danger)}}.table-wrap{{overflow:auto;border:1px solid var(--line);border-radius:13px;margin-top:16px}}table{{width:100%;border-collapse:collapse;min-width:720px;background:#091912}}th,td{{text-align:right;padding:12px 14px;border-bottom:1px solid #1e392f}}th{{color:#a7c3b8;font-size:12px;background:#10241c}}tr:last-child td{{border-bottom:0}}tr:hover td{{background:#10241c88}}code{{direction:ltr;display:inline-block;color:#c7f5e5}}.badge{{display:inline-flex;padding:3px 10px;border:1px solid #2c5b49;border-radius:99px;background:#17382b;color:#9cf0d1;font-size:12px}}form.inline{{display:inline}}form.inline button{{margin:0;min-height:34px;padding:5px 10px}}.quick-links a{{display:block;padding:13px;border:1px solid var(--line);border-radius:11px;background:#0a1913}}.progress{{height:8px;background:#07140f;border-radius:10px;overflow:hidden;margin-top:14px}}.progress span{{display:block;height:100%;border-radius:inherit;background:linear-gradient(90deg,var(--brand-2),var(--brand))}}.login-body{{display:grid;place-items:center;padding:24px;overflow:hidden}}.login-shell{{width:min(100%,440px);position:relative;z-index:1}}.login-card{{padding:34px;border-radius:24px;background:#0c1d16e8;box-shadow:0 30px 100px #0009;border:1px solid #315346;backdrop-filter:blur(18px)}}.login-logo{{display:grid;place-items:center;width:58px;height:58px;border-radius:17px;margin-bottom:24px;background:linear-gradient(145deg,var(--brand),var(--brand-2));color:#062019;font-size:30px;font-weight:bold;box-shadow:0 15px 35px #39ce9938}}.login-card h1{{font-size:25px;margin:0 0 5px}}.login-card button{{width:100%;margin-top:20px}}.login-note{{display:flex;align-items:center;gap:8px;margin-top:18px;color:var(--muted);font-size:11px}}.login-note i{{width:7px;height:7px;background:var(--brand);border-radius:50%}}
        @media(max-width:980px){{.app-body{{display:block}}aside{{position:sticky;height:auto;padding:12px 16px;border-left:0;border-bottom:1px solid var(--line)}}.brand{{padding:0 4px 10px;border:0}}.brand small,.side-status{{display:none}}nav{{flex-direction:row;overflow:auto;margin:0;padding-bottom:2px}}nav a{{flex:0 0 auto;padding:8px 11px}}.content{{padding:24px 18px 45px}}.grid{{grid-template-columns:repeat(2,1fr)}}}}@media(max-width:620px){{.topbar{{align-items:flex-start}}.topbar h1{{font-size:20px}}.live{{display:none}}.grid,.two,.row{{grid-template-columns:1fr}}.content{{padding:18px 13px 38px}}.card{{padding:17px;border-radius:15px}}nav a span{{font-size:12px}}.login-card{{padding:26px 21px}}}}
        .subnav{{display:flex;gap:7px;overflow:auto;margin:0 0 18px;padding:6px;border:1px solid var(--line);border-radius:14px;background:#091912}}.subnav a{{flex:0 0 auto;padding:9px 14px;color:var(--muted);border-radius:9px}}.subnav a:hover{{color:white;background:#10281f}}.subnav a.active{{color:#062019;background:linear-gradient(120deg,var(--brand),#39ce99);font-weight:bold}}
        </style></head><body class="{body_class}">{layout}</body></html>"""
        response = web.Response(text=markup, content_type="text/html", charset="utf-8")
        response.headers.update({
            "Cache-Control": "no-store", "X-Frame-Options": "DENY",
            "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'",
        })
        return response

    def login_page(self, message: str = "برای دسترسی به مرکز کنترل، رمز مدیر را وارد کنید.") -> web.Response:
        return self.page(f'''<section class="login-card"><div class="login-logo">↑</div><h1>ورود به FileFlow</h1><p class="muted">{html.escape(message)}</p><form method="post" action="/manage/login"><label for="password">رمز عبور</label><input id="password" name="password" type="password" autocomplete="current-password" placeholder="••••••••••••" autofocus required><button>ورود امن به داشبورد</button></form><div class="login-note"><i></i><span>نشست امن و محدود به ۳۰ دقیقه</span></div></section>''')

    @staticmethod
    def storage_tabs(active: str) -> str:
        tabs = (
            ("list", "/manage/storage", "فضاهای متصل"),
            ("routing", "/manage/storage/routing", "مسیر فایل‌های جدید"),
            ("new", "/manage/storage/new", "افزودن فضای جدید"),
        )
        return '<div class="subnav">' + "".join(
            f'<a class="{"active" if key == active else ""}" href="{url}">{label}</a>'
            for key, url, label in tabs
        ) + "</div>"

    async def storage_page(self, request: web.Request) -> web.Response:
        session = self.session(request)
        if not session:
            return self.login_page()
        pending_replications = await self.storage.pending_replication_count()
        backend_usage = await self.storage.backend_usage()
        cards_parts: list[str] = []
        for item in self.manager.backends.values():
            name = html.escape(item.name)
            refs = await self.storage.backend_reference_count(item.name)
            usage = backend_usage.get(item.name, 0)
            used_gb = usage / (1024 ** 3)
            capacity_gb = item.capacity_bytes / (1024 ** 3) if item.capacity_bytes else 0
            healthy = item.unhealthy_until <= time.monotonic()
            role_labels = {"primary": "Primary", "replica": "Replica Only", "download": "Download Only", "disabled": "Disabled"}
            state_class = "ok" if item.role != "disabled" and healthy else "bad"
            state = f'{role_labels[item.role]} • ' + ("سالم" if healthy else "موقتاً ناسالم")
            role_options = "".join(
                f'<option value="{value}" {"selected" if item.role == value else ""}>{label}</option>'
                for value, label in role_labels.items()
            )
            cards_parts.append(
                f'''<section class="card"><h2>{name}</h2><p>Bucket: <b>{html.escape(item.bucket)}</b></p><p>Endpoint: {html.escape(item.endpoint_url)}</p><p>Region: {html.escape(item.region)}</p><p>مصرف ثبت‌شده: {used_gb:.2f} GB از {'نامحدود' if not capacity_gb else f'{capacity_gb:.2f} GB'}</p><p>Latency: {item.latency_ms:.0f} ms | خطا: {item.failures} | ارجاع فایل: {refs}</p><p class="{state_class}">{state}</p><form method="post" action="/manage/storage/role"><input type="hidden" name="csrf" value="{session.csrf}"><input type="hidden" name="name" value="{name}"><label>نقش فضا</label><select name="role">{role_options}</select><button>ذخیره نقش</button></form><form method="post" action="/manage/storage/capacity"><input type="hidden" name="csrf" value="{session.csrf}"><input type="hidden" name="name" value="{name}"><div class="row"><div><label>ظرفیت قابل استفاده (GB، صفر=نامحدود)</label><input name="capacity_gb" type="number" value="{capacity_gb:.2f}" min="0" step="0.1"></div><div><label>فضای رزرو (GB)</label><input name="reserve_gb" type="number" value="{item.reserve_bytes / (1024 ** 3):.2f}" min="0" step="0.1"></div></div><button>ذخیره ظرفیت</button></form><form method="post" action="/manage/storage/priority"><input type="hidden" name="csrf" value="{session.csrf}"><input type="hidden" name="name" value="{name}"><label>اولویت</label><input name="priority" type="number" value="{item.priority}" min="1" max="9999"><button>ذخیره اولویت</button></form><form method="post" action="/manage/storage/delete"><input type="hidden" name="csrf" value="{session.csrf}"><input type="hidden" name="name" value="{name}"><button class="danger">حذف تنظیمات این فضا</button></form></section>'''
            )
        cards = (
            f'<section class="card"><p>کارهای replication در صف: <b>{pending_replications}</b></p></section>'
            + ("".join(cards_parts) or '<p class="muted">هنوز فضای S3 ثبت نشده است.</p>')
        )
        return self.page(self.storage_tabs("list") + cards, "storage", True)

    async def storage_routing_page(self, request: web.Request) -> web.Response:
        session = self.session(request)
        if not session:
            raise web.HTTPFound("/manage/")
        mode = await self.storage.get_setting("storage_backend", "local")
        replication_count = await self.storage.get_setting("replication_count", "1")
        migration_status = html.escape(self.migration_status)
        body = f'''{self.storage_tabs("routing")}<section class="card"><h2>مسیر فایل‌های جدید</h2><p class="muted">مقصد اصلی فایل‌ها و تعداد نسخه‌های پشتیبان را تعیین کنید.</p><p>حالت فعلی: <span class="badge">{html.escape(mode)}</span></p><form method="post" action="/manage/storage/mode"><input type="hidden" name="csrf" value="{session.csrf}"><label>مقصد فایل‌ها</label><select name="mode"><option value="s3">S3 هوشمند</option><option value="local">دیسک محلی</option></select><button>اعمال مسیر</button></form><form method="post" action="/manage/storage/replication"><input type="hidden" name="csrf" value="{session.csrf}"><label>تعداد کل نسخه‌های هر فایل</label><input name="count" type="number" value="{html.escape(replication_count)}" min="1" max="5"><button>ذخیره تعداد نسخه‌ها</button></form></section><section class="card"><h2>انتقال فایل‌های قدیمی</h2><p class="muted">فایل‌های محلی پس از آپلود موفق و ثبت در دیتابیس به S3 منتقل می‌شوند.</p><p>{migration_status}</p><form method="post" action="/manage/storage/migrate"><input type="hidden" name="csrf" value="{session.csrf}"><button>شروع انتقال امن به S3</button></form></section>'''
        return self.page(body, "storage", True)

    async def storage_add_page(self, request: web.Request) -> web.Response:
        session = self.session(request)
        if not session:
            raise web.HTTPFound("/manage/")
        body = f'''{self.storage_tabs("new")}<section class="card"><h2>افزودن فضای جدید</h2><p class="muted">یک سرویس S3-compatible جدید ثبت کنید. قبل از ذخیره، اتصال و Bucket آزمایش می‌شوند.</p><form method="post" action="/manage/storage/add"><input type="hidden" name="csrf" value="{session.csrf}"><div class="row"><div><label>نام یکتا</label><input name="name" placeholder="bunny-frankfurt" required></div><div><label>Region</label><input name="region" value="auto" required></div></div><label>S3 Endpoint HTTPS</label><input name="endpoint_url" type="url" placeholder="https://s3.example.com" required><label>Bucket</label><input name="bucket" required><div class="row"><div><label>Access Key</label><input name="access_key_id" autocomplete="off" required></div><div><label>Secret Key</label><input name="secret_access_key" type="password" autocomplete="new-password" required></div></div><div class="row"><div><label>ظرفیت قابل استفاده (GB، صفر=نامحدود)</label><input name="capacity_gb" type="number" value="0" min="0" step="0.1"></div><div><label>فضای رزرو (GB)</label><input name="reserve_gb" type="number" value="0" min="0" step="0.1"></div></div><label>اولویت (عدد کمتر بهتر)</label><input name="priority" type="number" value="100" min="1" max="9999"><button>تست اتصال و ذخیره</button></form></section>'''
        return self.page(body, "storage", True)

    async def index(self, request: web.Request) -> web.Response:
        session = self.session(request)
        if not session:
            return self.login_page()
        total, active_files, expired, active_bytes = await self.storage.statistics()
        pending = await self.storage.pending_replication_count()
        mode = await self.storage.get_setting("storage_backend", "local")
        disk = shutil.disk_usage(self.storage.data_dir)
        healthy = sum(
            1 for item in self.manager.backends.values()
            if item.enabled and item.unhealthy_until <= time.monotonic()
        )
        disk_percent = disk.used * 100 / disk.total
        body = f'''<section class="grid"><div class="card metric"><span>فایل‌های فعال</span><b>{active_files}</b><small class="muted">{total} رکورد کل</small></div><div class="card metric"><span>حجم فعال</span><b>{self._size(active_bytes)}</b><small class="muted">داده قابل دانلود</small></div><div class="card metric"><span>صف Replication</span><b>{pending}</b><small class="muted">کار در انتظار</small></div><div class="card metric"><span>ذخیره‌سازی سالم</span><b>{healthy}/{len(self.manager.backends)}</b><small class="muted">S3 backend</small></div></section><section class="two"><div class="card"><h2>وضعیت سرویس</h2><p><span class="badge">فعال</span> ربات و وب‌سرور در حال اجرا هستند.</p><p>مسیر فعلی: <b>{html.escape(mode)}</b></p><p>منقضی در انتظار پاک‌سازی: <b>{expired}</b></p><p class="muted">Uptime: {self._duration(time.monotonic() - self.started_at)}</p></div><div class="card"><h2>ظرفیت دیسک سرور</h2><p><b>{disk_percent:.1f}%</b> مصرف شده</p><div class="progress"><span style="width:{min(disk_percent, 100):.1f}%"></span></div><p class="muted">{self._size(disk.used)} از {self._size(disk.total)} • آزاد: {self._size(disk.free)}</p><a href="/manage/system">مشاهده جزئیات سیستم ←</a></div></section><section class="card"><h2>دسترسی سریع</h2><div class="row quick-links"><a href="/manage/files"><b>فایل‌ها</b><br><span class="muted">مشاهده و حذف فایل‌ها</span></a><a href="/manage/storage"><b>فضاهای ابری</b><br><span class="muted">ظرفیت و اولویت S3</span></a><a href="/manage/jobs"><b>صف انتقال</b><br><span class="muted">کنترل Replication</span></a><a href="/manage/settings"><b>تنظیمات</b><br><span class="muted">رمز و بکاپ</span></a></div><form method="post" action="/manage/logout"><input type="hidden" name="csrf" value="{session.csrf}"><button class="secondary">خروج امن</button></form></section>'''
        return self.page(body, "dashboard", True)

    async def files_page(self, request: web.Request) -> web.Response:
        session = self.session(request)
        if not session:
            raise web.HTTPFound("/manage/")
        rows = []
        now = int(time.time())
        for item in await self.storage.admin_files():
            state = "فعال" if item.expires_at > now else "منقضی"
            expires = datetime.fromtimestamp(item.expires_at).astimezone().strftime("%Y-%m-%d %H:%M")
            rows.append(f'''<tr><td>{html.escape(item.original_name)}</td><td>{self._size(item.size)}</td><td><span class="badge">{html.escape(item.backend_name)}</span></td><td>{item.replica_count} / صف {item.pending_count}</td><td>{expires}<br><span class="muted">{state}</span></td><td><a href="/d/{item.token}" target="_blank">نمایش</a> <form class="inline" method="post" action="/manage/files/delete"><input type="hidden" name="csrf" value="{session.csrf}"><input type="hidden" name="token" value="{item.token}"><button class="danger">حذف</button></form></td></tr>''')
        table = "".join(rows) or '<tr><td colspan="6" class="muted">فایلی ثبت نشده است.</td></tr>'
        return self.page(f'''<section class="card"><h2>فایل‌ها</h2><p class="muted">۱۰۰ فایل اخیر؛ حذف شامل نسخه اصلی، replicaها، صف و فایل موقت است.</p><div class="table-wrap"><table><thead><tr><th>نام</th><th>حجم</th><th>محل اصلی</th><th>نسخه‌ها</th><th>انقضا</th><th>عملیات</th></tr></thead><tbody>{table}</tbody></table></div></section>''', "files", True)

    async def jobs_page(self, request: web.Request) -> web.Response:
        session = self.session(request)
        if not session:
            raise web.HTTPFound("/manage/")
        paused = await self.storage.get_setting("replication_paused", "0") == "1"
        rows = []
        for job in await self.storage.admin_replication_jobs():
            retry = datetime.fromtimestamp(job.next_attempt_at).astimezone().strftime("%Y-%m-%d %H:%M:%S") if job.next_attempt_at else "اکنون"
            rows.append(f'''<tr><td>{job.id}</td><td>{html.escape(job.original_name)}</td><td>{html.escape(job.target_backend)}</td><td>{job.attempts}</td><td>{retry}</td><td>{html.escape(job.last_error or "—")}</td></tr>''')
        table = "".join(rows) or '<tr><td colspan="6" class="muted">صف خالی است.</td></tr>'
        state = "متوقف" if paused else "در حال اجرا"
        action = "ادامه صف" if paused else "توقف صف"
        return self.page(f'''<section class="card"><h2>صف انتقال پایدار</h2><p>وضعیت: <b>{state}</b></p><p class="muted">کارهای ناموفق بعد از restart باقی می‌مانند و با فاصله افزایشی دوباره اجرا می‌شوند.</p><div class="row"><form method="post" action="/manage/jobs/pause"><input type="hidden" name="csrf" value="{session.csrf}"><button class="secondary">{action}</button></form><form method="post" action="/manage/jobs/retry"><input type="hidden" name="csrf" value="{session.csrf}"><button>اجرای دوباره همه کارها</button></form></div><div class="table-wrap"><table><thead><tr><th>ID</th><th>فایل</th><th>مقصد</th><th>تلاش</th><th>اجرای بعد</th><th>آخرین خطا</th></tr></thead><tbody>{table}</tbody></table></div></section>''', "jobs", True)

    async def system_page(self, request: web.Request) -> web.Response:
        session = self.session(request)
        if not session:
            raise web.HTTPFound("/manage/")
        disk = shutil.disk_usage(self.storage.data_dir)
        load = os.getloadavg() if hasattr(os, "getloadavg") else (0.0, 0.0, 0.0)
        body = f'''<section class="grid"><div class="card metric"><span>دیسک آزاد</span><b>{self._size(disk.free)}</b></div><div class="card metric"><span>مصرف دیسک</span><b>{disk.used * 100 / disk.total:.1f}%</b></div><div class="card metric"><span>Load 1m</span><b>{load[0]:.2f}</b></div><div class="card metric"><span>Uptime پردازش</span><b>{self._duration(time.monotonic() - self.started_at)}</b></div></section><section class="card"><h2>وضعیت backendها</h2>{''.join(f'<p><b>{html.escape(item.name)}</b> — {"فعال" if item.enabled else "غیرفعال"} — latency {item.latency_ms:.0f}ms — خطا {item.failures}</p>' for item in self.manager.backends.values()) or '<p class="muted">S3 ثبت نشده است.</p>'}</section>'''
        return self.page(body, "system", True)

    async def audit_page(self, request: web.Request) -> web.Response:
        if not self.session(request):
            raise web.HTTPFound("/manage/")
        rows = []
        for created_at, actor, action, detail in await self.storage.recent_audit():
            created = datetime.fromtimestamp(created_at).astimezone().strftime("%Y-%m-%d %H:%M:%S")
            rows.append(
                f"<tr><td>{created}</td><td><code>{html.escape(actor)}</code></td>"
                f"<td><code>{html.escape(action)}</code></td><td>{html.escape(detail or '—')}</td></tr>"
            )
        table = "".join(rows) or '<tr><td colspan="4" class="muted">رویدادی ثبت نشده است.</td></tr>'
        return self.page(
            f'''<section class="card"><h2>رویدادهای مدیریتی</h2><p class="muted">۲۰۰ عملیات معتبر اخیر همراه IP و زمان ثبت می‌شود؛ رمزها و کلیدها در گزارش قرار نمی‌گیرند.</p><div class="table-wrap"><table><thead><tr><th>زمان</th><th>IP</th><th>عملیات</th><th>جزئیات</th></tr></thead><tbody>{table}</tbody></table></div></section>''',
            "audit", True,
        )

    async def settings_page(self, request: web.Request) -> web.Response:
        session = self.session(request)
        if not session:
            raise web.HTTPFound("/manage/")
        backup_dir = self.storage.data_dir / "backups"
        backups = sorted(backup_dir.glob("admin-*.sqlite3"), reverse=True)[:10] if backup_dir.exists() else []
        backup_links = "".join(
            f'<p><a href="/manage/backups/{item.name}">{html.escape(item.name)}</a> — {self._size(item.stat().st_size)}</p>'
            for item in backups
        ) or '<p class="muted">هنوز بکاپی ساخته نشده است.</p>'
        body = f'''<section class="two"><div class="card"><h2>تغییر رمز پنل</h2><form method="post" action="/manage/settings/password"><input type="hidden" name="csrf" value="{session.csrf}"><label>رمز فعلی</label><input name="current_password" type="password" autocomplete="current-password" required><label>رمز جدید</label><input name="new_password" type="password" minlength="12" autocomplete="new-password" required><label>تکرار رمز جدید</label><input name="confirm_password" type="password" minlength="12" autocomplete="new-password" required><button>تغییر رمز و خروج سایر نشست‌ها</button></form></div><div class="card"><h2>بکاپ دیتابیس</h2><p class="muted">Snapshot سازگار SQLite؛ شامل فایل‌های حجیم و کلیدهای S3 نیست.</p><form method="post" action="/manage/backups/create"><input type="hidden" name="csrf" value="{session.csrf}"><button>ساخت بکاپ جدید</button></form>{backup_links}</div></section>'''
        return self.page(body, "settings", True)

    async def change_password(self, request: web.Request) -> web.Response:
        session, data = await self.require_form_session(request)
        current = data.get("current_password", "")
        new = data.get("new_password", "")
        confirmation = data.get("confirm_password", "")
        if not verify_password(current, self.password_hash):
            raise web.HTTPBadRequest(text="Current password is incorrect")
        if len(new) < 12 or not secrets.compare_digest(new, confirmation):
            raise web.HTTPBadRequest(text="New password is invalid or does not match")
        encoded = hash_password(new)
        await self.storage.set_setting("admin_web_password_hash", encoded)
        self.password_hash = encoded
        token = request.cookies.get("admin_session", "")
        session.csrf = secrets.token_urlsafe(24)
        self.sessions = {token: session}
        await self.storage.add_audit(request.remote or "unknown", "password_changed")
        raise web.HTTPFound("/manage/settings")

    async def create_backup(self, request: web.Request) -> web.Response:
        await self.require_form_session(request)
        backup_dir = self.storage.data_dir / "backups"
        filename = f"admin-{datetime.now().astimezone().strftime('%Y%m%d-%H%M%S')}.sqlite3"
        await self.storage.create_database_backup(backup_dir / filename)
        backups = sorted(backup_dir.glob("admin-*.sqlite3"), reverse=True)
        for old in backups[10:]:
            old.unlink(missing_ok=True)
        raise web.HTTPFound("/manage/settings")

    async def download_backup(self, request: web.Request) -> web.StreamResponse:
        if not self.session(request):
            raise web.HTTPForbidden(text="Authentication required")
        name = request.match_info["name"]
        if not re.fullmatch(r"admin-\d{8}-\d{6}\.sqlite3", name):
            raise web.HTTPNotFound()
        path = self.storage.data_dir / "backups" / name
        if not path.is_file():
            raise web.HTTPNotFound()
        response = web.FileResponse(path)
        response.headers["Content-Disposition"] = f'attachment; filename="{name}"'
        response.headers["Cache-Control"] = "no-store"
        return response

    async def delete_file(self, request: web.Request) -> web.Response:
        _, data = await self.require_form_session(request)
        item = await self.storage.get(data.get("token", ""))
        if item:
            try:
                if item.backend_name != "local" and item.object_key:
                    await self.manager.delete(item.backend_name, item.object_key)
                for backend, key in await self.storage.replicas_for(item.token):
                    await self.manager.delete(backend, key)
                await self.storage.delete_stored_file(item.stored_name)
                await self.storage.delete_record(item.token)
            except Exception:
                logger.exception("Admin could not delete file %s", item.token)
                raise web.HTTPInternalServerError(text="File deletion failed")
        raise web.HTTPFound("/manage/files")

    async def retry_jobs(self, request: web.Request) -> web.Response:
        await self.require_form_session(request)
        await self.storage.retry_replication_jobs()
        raise web.HTTPFound("/manage/jobs")

    async def toggle_replication_pause(self, request: web.Request) -> web.Response:
        await self.require_form_session(request)
        paused = await self.storage.get_setting("replication_paused", "0") == "1"
        await self.storage.set_setting("replication_paused", "0" if paused else "1")
        raise web.HTTPFound("/manage/jobs")

    @staticmethod
    def _size(size: int) -> str:
        value = float(size)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if value < 1024 or unit == "TB":
                return f"{value:.1f} {unit}"
            value /= 1024
        return f"{size} B"

    @staticmethod
    def _duration(seconds: float) -> str:
        minutes = int(seconds // 60)
        hours, minutes = divmod(minutes, 60)
        days, hours = divmod(hours, 24)
        return f"{days}d {hours}h {minutes}m"

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
            return self.login_page("رمز عبور نادرست است؛ دوباره تلاش کنید.")
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
        await self.storage.add_audit(request.remote or "unknown", request.path)
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
            capacity_gb = float(data.get("capacity_gb", "0"))
            reserve_gb = float(data.get("reserve_gb", "0"))
        except ValueError:
            return self.page('<section class="card"><p class="bad">اولویت یا ظرفیت معتبر نیست.</p></section>')
        if capacity_gb < 0 or reserve_gb < 0 or (capacity_gb and reserve_gb >= capacity_gb):
            return self.page('<section class="card"><p class="bad">ظرفیت نامعتبر است یا فضای رزرو از ظرفیت کمتر نیست.</p></section>')
        config: dict[str, object] = {
            "name": data.get("name", ""), "endpoint_url": endpoint,
            "bucket": data.get("bucket", ""), "region": data.get("region", "auto"),
            "access_key_id": data.get("access_key_id", ""),
            "secret_access_key": data.get("secret_access_key", ""),
            "priority": priority, "enabled": True, "role": "primary",
            "capacity_bytes": int(capacity_gb * 1024 ** 3),
            "reserve_bytes": int(reserve_gb * 1024 ** 3),
        }
        probe = ObjectStorageManager((config,), self.manager.transfer_config.multipart_chunksize // (1024 * 1024), self.manager.presign_seconds)
        backend = next(iter(probe.backends.values()))
        if not await probe.health_check(backend):
            return self.page('<section class="card"><p class="bad">اتصال یا دسترسی Bucket ناموفق بود؛ چیزی ذخیره نشد.</p></section>')
        configs = self.manager.export_configs() + [config]
        await self.store.save(configs)
        self.manager.replace_configs(configs)
        await self.storage.set_setting("storage_backend", "s3")
        raise web.HTTPFound("/manage/storage")

    async def set_storage_mode(self, request: web.Request) -> web.Response:
        _, data = await self.require_form_session(request)
        mode = data.get("mode", "")
        if mode not in {"local", "s3"}:
            raise web.HTTPBadRequest(text="Invalid storage mode")
        if mode == "s3" and not self.manager.backends:
            return self.page('<section class="card"><p class="bad">ابتدا یک فضای S3 سالم اضافه کنید.</p></section>')
        await self.storage.set_setting("storage_backend", mode)
        raise web.HTTPFound("/manage/storage/routing")

    async def set_replication(self, request: web.Request) -> web.Response:
        _, data = await self.require_form_session(request)
        try:
            count = int(data.get("count", "1"))
        except ValueError:
            raise web.HTTPBadRequest(text="Invalid replication count")
        if not 1 <= count <= 5:
            raise web.HTTPBadRequest(text="Replication count must be between 1 and 5")
        await self.storage.set_setting("replication_count", str(count))
        raise web.HTTPFound("/manage/storage/routing")

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
                config["role"] = "disabled" if config.get("role", "primary") != "disabled" else "primary"
                config["enabled"] = config["role"] != "disabled"
        await self._save_backend_configs(configs)
        if not any(item.enabled for item in self.manager.backends.values()):
            await self.storage.set_setting("storage_backend", "local")
        raise web.HTTPFound("/manage/storage")

    async def set_storage_role(self, request: web.Request) -> web.Response:
        _, data = await self.require_form_session(request)
        name = data.get("name", "")
        role = data.get("role", "")
        if name not in self.manager.backends:
            raise web.HTTPNotFound(text="Storage backend not found")
        if role not in {"primary", "replica", "download", "disabled"}:
            raise web.HTTPBadRequest(text="Invalid storage role")
        configs = self.manager.export_configs()
        for config in configs:
            if config["name"] == name:
                config["role"] = role
                config["enabled"] = role != "disabled"
        await self._save_backend_configs(configs)
        if not any(item.role == "primary" for item in self.manager.backends.values()):
            await self.storage.set_setting("storage_backend", "local")
        raise web.HTTPFound("/manage/storage")

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
        raise web.HTTPFound("/manage/storage")

    async def set_storage_capacity(self, request: web.Request) -> web.Response:
        _, data = await self.require_form_session(request)
        name = data.get("name", "")
        if name not in self.manager.backends:
            raise web.HTTPNotFound(text="Storage backend not found")
        try:
            capacity_gb = float(data.get("capacity_gb", "0"))
            reserve_gb = float(data.get("reserve_gb", "0"))
        except ValueError:
            raise web.HTTPBadRequest(text="Invalid capacity")
        if capacity_gb < 0 or reserve_gb < 0 or (capacity_gb and reserve_gb >= capacity_gb):
            raise web.HTTPBadRequest(text="Reserve must be smaller than capacity")
        configs = self.manager.export_configs()
        for config in configs:
            if config["name"] == name:
                config["capacity_bytes"] = int(capacity_gb * 1024 ** 3)
                config["reserve_bytes"] = int(reserve_gb * 1024 ** 3)
        await self._save_backend_configs(configs)
        raise web.HTTPFound("/manage/storage")

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
        raise web.HTTPFound("/manage/storage")

    async def start_migration(self, request: web.Request) -> web.Response:
        await self.require_form_session(request)
        if not self.manager.backends:
            return self.page('<section class="card"><p class="bad">ابتدا حداقل یک فضای S3 اضافه کنید.</p></section>')
        if self.migration_task and not self.migration_task.done():
            raise web.HTTPFound("/manage/storage/routing")
        self.migration_status = "در حال آماده‌سازی فهرست فایل‌ها…"
        self.migration_task = asyncio.create_task(self._migrate_local_files())
        raise web.HTTPFound("/manage/storage/routing")

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
                    backend_usage = await self.storage.backend_usage()
                    primary = await self.manager.upload(
                        source, object_key, item.mime_type, usage=backend_usage
                    )
                    uploaded.append((primary, object_key))
                    replicas = await self.manager.replicate(
                        source, object_key, item.mime_type, primary, desired_total,
                        backend_usage,
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
