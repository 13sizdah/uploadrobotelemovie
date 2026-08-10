from __future__ import annotations

import html
from datetime import datetime


def render_download_page(
    *,
    file_name: str,
    file_size: str,
    mime_type: str,
    expires_at: int,
    download_url: str,
) -> str:
    safe_name = html.escape(file_name)
    safe_size = html.escape(file_size)
    safe_type = html.escape(mime_type)
    safe_url = html.escape(download_url, quote=True)
    expiry = html.escape(
        datetime.fromtimestamp(expires_at).astimezone().strftime("%Y/%m/%d، ساعت %H:%M")
    )
    return PAGE.replace("{{FILE_NAME}}", safe_name).replace(
        "{{FILE_SIZE}}", safe_size
    ).replace("{{MIME_TYPE}}", safe_type).replace(
        "{{EXPIRY}}", expiry
    ).replace("{{DOWNLOAD_URL}}", safe_url)


PAGE = """<!doctype html>
<html lang="fa" dir="rtl">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="robots" content="noindex, nofollow, noarchive">
  <meta name="theme-color" content="#07152b">
  <title>دانلود {{FILE_NAME}}</title>
  <style>
    :root {
      color-scheme: dark;
      --navy: #07152b;
      --navy-2: #0b1d38;
      --card: rgba(13, 32, 58, .88);
      --line: rgba(158, 181, 218, .18);
      --text: #f7faff;
      --muted: #b8c6db;
      --cyan: #43d6c5;
      --cyan-dark: #081f24;
      --focus: #8cf7ea;
    }
    * { box-sizing: border-box; }
    html, body { min-height: 100%; }
    body {
      margin: 0;
      min-width: 280px;
      background:
        radial-gradient(circle at 82% 8%, rgba(34, 126, 171, .28), transparent 32rem),
        radial-gradient(circle at 8% 92%, rgba(30, 176, 143, .14), transparent 30rem),
        var(--navy);
      color: var(--text);
      font-family: Tahoma, Arial, sans-serif;
      font-size: 16px;
      line-height: 1.7;
      display: grid;
      place-items: center;
      padding: 24px;
    }
    .shell { width: min(100%, 620px); }
    .brand {
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 10px;
      margin-bottom: 18px;
      color: var(--muted);
      font-size: .92rem;
    }
    .brand svg { color: var(--cyan); }
    .card {
      position: relative;
      overflow: hidden;
      padding: clamp(24px, 6vw, 44px);
      border: 1px solid var(--line);
      border-radius: 28px;
      background: var(--card);
      box-shadow: 0 28px 80px rgba(0, 0, 0, .38);
      backdrop-filter: blur(18px);
    }
    .card::before {
      content: "";
      position: absolute;
      inset: 0 0 auto;
      height: 3px;
      background: linear-gradient(90deg, transparent, var(--cyan), transparent);
    }
    .file-icon {
      width: 76px;
      height: 76px;
      margin: 0 auto 22px;
      display: grid;
      place-items: center;
      border: 1px solid rgba(67, 214, 197, .28);
      border-radius: 22px;
      background: rgba(67, 214, 197, .09);
      color: var(--cyan);
    }
    h1 {
      margin: 0;
      font-size: clamp(1.35rem, 4vw, 1.8rem);
      line-height: 1.5;
      text-align: center;
    }
    .lead { margin: 8px 0 28px; color: var(--muted); text-align: center; }
    .details {
      margin: 0 0 26px;
      padding: 6px 18px;
      border: 1px solid var(--line);
      border-radius: 18px;
      background: rgba(3, 14, 29, .35);
    }
    .row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 18px;
      min-height: 52px;
      border-bottom: 1px solid var(--line);
    }
    .row:last-child { border-bottom: 0; }
    .label { flex: 0 0 auto; color: var(--muted); }
    .value {
      min-width: 0;
      overflow-wrap: anywhere;
      text-align: left;
      direction: ltr;
      font-weight: 700;
    }
    .download {
      width: 100%;
      min-height: 56px;
      border: 1px solid transparent;
      border-radius: 16px;
      background: var(--cyan);
      color: var(--cyan-dark);
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 10px;
      text-decoration: none;
      font-weight: 800;
      cursor: pointer;
      box-shadow: 0 12px 30px rgba(42, 204, 185, .2);
      transition: background-color 180ms ease, box-shadow 180ms ease, transform 180ms ease;
    }
    .download:hover { background: #68e5d6; box-shadow: 0 15px 36px rgba(42, 204, 185, .3); }
    .download:active { transform: translateY(1px); }
    .download:focus-visible { outline: 3px solid var(--focus); outline-offset: 4px; }
    .download[aria-disabled="true"] { pointer-events: none; opacity: .78; }
    .notice {
      margin: 18px 0 0;
      display: flex;
      align-items: flex-start;
      justify-content: center;
      gap: 8px;
      color: var(--muted);
      font-size: .86rem;
      text-align: center;
    }
    .notice svg { flex: 0 0 auto; margin-top: 3px; }
    @media (max-width: 480px) {
      body { padding: 14px; }
      .card { border-radius: 22px; }
      .row { align-items: flex-start; flex-direction: column; gap: 2px; padding: 11px 0; }
      .value { width: 100%; text-align: right; }
    }
    @media (prefers-reduced-motion: reduce) {
      *, *::before, *::after { scroll-behavior: auto !important; transition: none !important; }
    }
  </style>
</head>
<body>
  <main class="shell">
    <div class="brand" aria-label="سرویس دانلود امن">
      <svg aria-hidden="true" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10Z"/><path d="m9 12 2 2 4-4"/></svg>
      <span>دانلود امن فایل</span>
    </div>
    <section class="card" aria-labelledby="page-title">
      <div class="file-icon">
        <svg aria-hidden="true" width="38" height="38" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z"/><path d="M14 2v6h6M12 18v-6m-3 3 3 3 3-3"/></svg>
      </div>
      <h1 id="page-title">فایل آماده دانلود است</h1>
      <p class="lead">پیش از دانلود، مشخصات فایل را بررسی کنید.</p>
      <div class="details" aria-label="مشخصات فایل">
        <div class="row"><span class="label">نام فایل</span><span class="value">{{FILE_NAME}}</span></div>
        <div class="row"><span class="label">حجم</span><span class="value">{{FILE_SIZE}}</span></div>
        <div class="row"><span class="label">نوع فایل</span><span class="value">{{MIME_TYPE}}</span></div>
        <div class="row"><span class="label">اعتبار تا</span><span class="value">{{EXPIRY}}</span></div>
      </div>
      <a class="download" id="download-button" href="{{DOWNLOAD_URL}}" download>
        <svg aria-hidden="true" width="23" height="23" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"/></svg>
        <span id="button-label" role="status" aria-live="polite">شروع دانلود</span>
      </a>
      <p class="notice">
        <svg aria-hidden="true" width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="9"/><path d="M12 11v5m0-8h.01"/></svg>
        لینک پس از زمان اعلام‌شده منقضی و فایل به‌صورت خودکار حذف می‌شود.
      </p>
    </section>
  </main>
  <script>
    const button = document.getElementById('download-button');
    const label = document.getElementById('button-label');
    button.addEventListener('click', () => {
      label.textContent = 'در حال آغاز دانلود…';
      button.setAttribute('aria-disabled', 'true');
      button.setAttribute('aria-busy', 'true');
      window.setTimeout(() => {
        label.textContent = 'دانلود دوباره';
        button.removeAttribute('aria-disabled');
        button.removeAttribute('aria-busy');
      }, 2500);
    });
  </script>
</body>
</html>"""
