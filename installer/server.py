#!/usr/bin/env python3
from __future__ import annotations

import html
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PROJECT_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = PROJECT_DIR / ".installer"
LOG_FILE = STATE_DIR / "install.log"
DONE_FILE = STATE_DIR / "installed"
SETUP_TOKEN = os.environ.get("SETUP_TOKEN") or secrets.token_urlsafe(18)
PORT = int(os.environ.get("INSTALLER_PORT", "9090"))
LOCK = threading.Lock()
INSTALLING = False

DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,63}$")
BOT_TOKEN_RE = re.compile(r"^\d{6,15}:[A-Za-z0-9_-]{30,}$")
API_HASH_RE = re.compile(r"^[a-fA-F0-9]{32}$")


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def run_logged(args: list[str], *, input_text: str | None = None) -> None:
    with LOG_FILE.open("a", encoding="utf-8") as log:
        log.write("\n$ " + " ".join(args[:2]) + " …\n")
        log.flush()
        process = subprocess.run(
            args,
            cwd=PROJECT_DIR,
            input=input_text,
            text=True,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=False,
        )
        if process.returncode:
            raise RuntimeError(f"Command failed: {' '.join(args[:2])} (code {process.returncode})")


def env_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "") + '"'


def install(config: dict[str, str]) -> None:
    global INSTALLING
    try:
        STATE_DIR.mkdir(mode=0o700, exist_ok=True)
        LOG_FILE.write_text("شروع نصب…\n", encoding="utf-8")
        env_lines = [
            f"BOT_TOKEN={env_quote(config['bot_token'])}",
            f"PUBLIC_BASE_URL={env_quote('https://' + config['domain'])}",
            f"FILE_TTL_HOURS={config['ttl']}",
            "MAX_FILE_SIZE_MB=0",
            "HOST=0.0.0.0",
            "PORT=8080",
            "DATA_DIR=/app/data",
            "CLEANUP_INTERVAL_SECONDS=300",
            f"ALLOWED_USER_IDS={env_quote(config['allowed_ids'])}",
            f"TELEGRAM_API_ID={config['api_id']}",
            f"TELEGRAM_API_HASH={env_quote(config['api_hash'])}",
            "TELEGRAM_API_BASE=http://telegram-bot-api:8081",
        ]
        env_path = PROJECT_DIR / ".env"
        env_path.write_text("\n".join(env_lines) + "\n", encoding="utf-8")
        env_path.chmod(0o600)

        run_logged(["docker", "compose", "pull"])
        run_logged(["docker", "compose", "up", "-d", "--build"])

        nginx_config = f"""server {{
    listen 80;
    server_name {config['domain']};
    client_max_body_size 0;
    location / {{
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_request_buffering off;
    }}
}}
"""
        nginx_target = Path("/etc/nginx/sites-available/telegram-file-link-bot")
        nginx_target.write_text(nginx_config, encoding="utf-8")
        enabled = Path("/etc/nginx/sites-enabled/telegram-file-link-bot")
        if not enabled.exists():
            enabled.symlink_to(nginx_target)
        run_logged(["nginx", "-t"])
        run_logged(["systemctl", "reload", "nginx"])

        if config["ssl"] == "yes":
            run_logged([
                "certbot", "--nginx", "--non-interactive", "--agree-tos",
                "--redirect", "-m", config["email"], "-d", config["domain"],
            ])

        DONE_FILE.write_text(str(int(time.time())), encoding="ascii")
        with LOG_FILE.open("a", encoding="utf-8") as log:
            log.write("\nINSTALL_COMPLETE\n")
    except Exception as exc:
        with LOG_FILE.open("a", encoding="utf-8") as log:
            log.write(f"\nINSTALL_ERROR: {exc}\n")
    finally:
        with LOCK:
            INSTALLING = False


STYLE = """
:root{font-family:Vazirmatn,Tahoma,sans-serif;color:#e8eef9;background:#07111f}*{box-sizing:border-box}
body{margin:0;min-height:100vh;background:radial-gradient(circle at 80% 0,#17375d 0,transparent 38%),#07111f}
.wrap{max-width:780px;margin:0 auto;padding:38px 18px}.card{background:#101d2e;border:1px solid #263b55;border-radius:20px;padding:28px;box-shadow:0 20px 70px #0008}
h1{font-size:26px;margin:0 0 8px}.muted{color:#9eb0c8;line-height:1.9}.steps{display:flex;gap:8px;margin:24px 0}.dot{height:7px;flex:1;border-radius:5px;background:#29394d}.dot.on{background:#35d39a}
label{display:block;margin:18px 0 7px;color:#c8d5e6}input,select{width:100%;padding:13px;border-radius:10px;border:1px solid #354a65;background:#0a1625;color:white;font-size:15px;direction:ltr}
.ltr{direction:ltr}.row{display:grid;grid-template-columns:1fr 1fr;gap:14px}.actions{display:flex;gap:10px;margin-top:25px}.btn{border:0;border-radius:10px;padding:12px 20px;font-size:15px;cursor:pointer;background:#35d39a;color:#042018;font-weight:bold}.secondary{background:#263a52;color:#fff}.error{background:#51252b;color:#ffc8ce;padding:12px;border-radius:10px;margin:12px 0}.ok{color:#55e6ae}.check{padding:10px 0;border-bottom:1px solid #26384d}
pre{direction:ltr;text-align:left;background:#050b13;padding:16px;border-radius:12px;max-height:330px;overflow:auto;white-space:pre-wrap;color:#b9d4c8}.hide{display:none}@media(max-width:600px){.row{grid-template-columns:1fr}.card{padding:20px}}
"""


def page(body: str, step: int = 1) -> bytes:
    dots = "".join(f'<span class="dot {"on" if i <= step else ""}"></span>' for i in range(1, 6))
    return f"""<!doctype html><html lang="fa" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>نصب ربات فایل</title><style>{STYLE}</style></head><body><main class="wrap"><section class="card"><h1>نصب ربات تبدیل فایل به لینک</h1><p class="muted">راه‌اندازی امن و مرحله‌به‌مرحله روی سرور لینوکس</p><div class="steps">{dots}</div>{body}</section></main></body></html>""".encode()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write("installer: " + fmt % args + "\n")

    def send_html(self, body: str, step: int = 1, status: int = 200) -> None:
        data = page(body, step)
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def authorized(self, query: dict[str, list[str]]) -> bool:
        return secrets.compare_digest(query.get("key", [""])[0], SETUP_TOKEN)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if not self.authorized(query):
            self.send_html("<div class='error'>کد نصب معتبر نیست. لینک کامل نمایش‌داده‌شده در ترمینال را باز کنید.</div>", status=403)
            return
        if parsed.path == "/status":
            log = LOG_FILE.read_text(encoding="utf-8", errors="replace") if LOG_FILE.exists() else "در انتظار شروع…"
            payload = json.dumps({"log": log, "done": DONE_FILE.exists(), "running": INSTALLING}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        checks = {
            "Python 3": sys.version_info >= (3, 10),
            "Docker": command_exists("docker"),
            "Nginx": command_exists("nginx"),
            "Certbot": command_exists("certbot"),
            "دسترسی root": os.geteuid() == 0,
        }
        check_html = "".join(f'<div class="check"><span class="{"ok" if ok else "error-text"}">{"✓" if ok else "✗"}</span> {html.escape(name)}</div>' for name, ok in checks.items())
        if DONE_FILE.exists():
            self.send_html("<h2 class='ok'>نصب قبلاً با موفقیت انجام شده است</h2><p class='muted'>برای امنیت، سرویس نصب‌کننده را متوقف کنید.</p>", 5)
            return
        self.send_html(f"""<h2>مرحله ۱: بررسی پیش‌نیازها</h2>{check_html}<p class="muted">اگر موردی قرمز است، اسکریپت bootstrap را دوباره با sudo اجرا کنید.</p><div class="actions"><button class="btn" onclick="wizard(2)">ادامه</button></div><div id="wizard"></div>{self.script()}""", 1)

    def script(self) -> str:
        key = html.escape(SETUP_TOKEN, quote=True)
        return f"""<script>
const key={json.dumps(SETUP_TOKEN)}; const root=document.getElementById('wizard');
function dots(n){{document.querySelectorAll('.dot').forEach((x,i)=>x.classList.toggle('on',i<n))}}
function wizard(n){{dots(n); if(n===2) root.innerHTML=`<h2>مرحله ۲: اطلاعات تلگرام</h2><label>توکن ربات از BotFather</label><input id="bot_token" placeholder="123456:ABC..."><div class="row"><div><label>API ID از my.telegram.org</label><input id="api_id" inputmode="numeric"></div><div><label>API Hash</label><input id="api_hash"></div></div><div class="actions"><button class="btn" onclick="wizard(3)">ادامه</button></div>`;
if(n===3) root.innerHTML=`<h2>مرحله ۳: دامنه و نگهداری</h2><label>دامنه متصل‌شده به IP سرور</label><input id="domain" placeholder="download.example.com"><div class="row"><div><label>مدت نگهداری فایل (ساعت)</label><input id="ttl" type="number" min="1" value="24"></div><div><label>شناسه کاربران مجاز (اختیاری)</label><input id="allowed_ids" placeholder="12345,67890"></div></div><label>فعال‌سازی خودکار HTTPS</label><select id="ssl"><option value="yes">بله، گواهی رایگان بگیر</option><option value="no">خیر، بعداً انجام می‌دهم</option></select><label>ایمیل برای گواهی SSL</label><input id="email" type="email" placeholder="admin@example.com"><div class="actions"><button class="btn" onclick="wizard(4)">بررسی نهایی</button></div>`;
if(n===4) root.innerHTML=`<h2>مرحله ۴: تأیید</h2><p class="muted">Docker، ربات، Local Bot API و Nginx راه‌اندازی خواهند شد. دامنه باید هم‌اکنون به IP این سرور اشاره کند.</p><div id="err"></div><div class="actions"><button class="btn" onclick="startInstall()">شروع نصب</button></div>`;}}
let values={{}}; document.addEventListener('input',e=>{{if(e.target.id)values[e.target.id]=e.target.value}}); document.addEventListener('change',e=>{{if(e.target.id)values[e.target.id]=e.target.value}});
async function startInstall(){{values.ssl=values.ssl||'yes';values.ttl=values.ttl||'24'; let r=await fetch('/install?key='+encodeURIComponent(key),{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(values)}});let j=await r.json();if(!r.ok){{document.getElementById('err').innerHTML='<div class="error">'+j.error+'</div>';return}};dots(5);root.innerHTML='<h2>مرحله ۵: در حال نصب</h2><pre id="log">شروع…</pre>';poll()}}
async function poll(){{let r=await fetch('/status?key='+encodeURIComponent(key));let j=await r.json();document.getElementById('log').textContent=j.log;document.getElementById('log').scrollTop=999999;if(!j.done&&j.running)setTimeout(poll,1500);else if(j.done)root.insertAdjacentHTML('beforeend','<h3 class="ok">نصب با موفقیت تمام شد ✓</h3>')}}
</script>"""

    def do_POST(self) -> None:
        global INSTALLING
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if parsed.path != "/install" or not self.authorized(query):
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if DONE_FILE.exists():
            self.send_json({"error": "نصب قبلاً انجام شده و صفحه نصب قفل است"}, 409)
            return
        try:
            length = min(int(self.headers.get("Content-Length", "0")), 20_000)
            config = json.loads(self.rfile.read(length))
            errors = []
            if not BOT_TOKEN_RE.fullmatch(str(config.get("bot_token", ""))): errors.append("توکن ربات معتبر نیست")
            if not str(config.get("api_id", "")).isdigit(): errors.append("API ID معتبر نیست")
            if not API_HASH_RE.fullmatch(str(config.get("api_hash", ""))): errors.append("API Hash باید ۳۲ کاراکتر هگز باشد")
            if not DOMAIN_RE.fullmatch(str(config.get("domain", ""))): errors.append("دامنه معتبر نیست")
            ttl = int(config.get("ttl", 0))
            if not 1 <= ttl <= 8760: errors.append("زمان نگهداری باید بین ۱ و ۸۷۶۰ ساعت باشد")
            allowed = str(config.get("allowed_ids", ""))
            if allowed and not re.fullmatch(r"\d+(?:,\d+)*", allowed): errors.append("شناسه‌های کاربران معتبر نیست")
            if config.get("ssl") == "yes" and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", str(config.get("email", ""))): errors.append("ایمیل SSL معتبر نیست")
            if os.geteuid() != 0: errors.append("نصب‌کننده باید با sudo اجرا شود")
            for executable in ("docker", "nginx", "systemctl"):
                if not command_exists(executable): errors.append(f"{executable} نصب نیست")
            if config.get("ssl") == "yes" and not command_exists("certbot"): errors.append("certbot نصب نیست")
            if errors:
                self.send_json({"error": "، ".join(errors)}, 400)
                return
            config = {k: str(config.get(k, "")).strip() for k in ("bot_token", "api_id", "api_hash", "domain", "ttl", "allowed_ids", "ssl", "email")}
            with LOCK:
                if INSTALLING:
                    self.send_json({"error": "نصب در حال اجراست"}, 409)
                    return
                INSTALLING = True
            threading.Thread(target=install, args=(config,), daemon=True).start()
            self.send_json({"started": True})
        except (ValueError, json.JSONDecodeError):
            self.send_json({"error": "اطلاعات ارسالی ناقص است"}, 400)

    def send_json(self, value: dict[str, object], status: int = 200) -> None:
        data = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    STATE_DIR.mkdir(mode=0o700, exist_ok=True)
    print("\nنصب‌کننده آماده است. این لینک را در مرورگر باز کنید:")
    print(f"http://SERVER-IP:{PORT}/?key={SETUP_TOKEN}\n", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
