from __future__ import annotations

import asyncio
import logging
import mimetypes
import secrets
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import aiofiles
import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    MessageOriginChannel,
    MessageOriginUser,
)
from aiogram.utils.markdown import hbold, hcode, hlink

from .config import Settings
from .download_page import render_download_page, render_expired_page
from .storage import Storage, StoredFile

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IncomingFile:
    downloadable: Any
    file_size: int | None
    file_name: str
    mime_type: str


def incoming_file(message: Message) -> IncomingFile | None:
    """Return a normalized downloadable file for every Telegram media type."""
    if message.document:
        item = message.document
        return IncomingFile(item, item.file_size, item.file_name or "document", item.mime_type or "application/octet-stream")
    if message.video:
        item = message.video
        return IncomingFile(item, item.file_size, item.file_name or "video.mp4", item.mime_type or "video/mp4")
    if message.audio:
        item = message.audio
        return IncomingFile(item, item.file_size, item.file_name or "audio.mp3", item.mime_type or "audio/mpeg")
    if message.animation:
        item = message.animation
        return IncomingFile(item, item.file_size, item.file_name or "animation.mp4", item.mime_type or "video/mp4")
    if message.voice:
        item = message.voice
        return IncomingFile(item, item.file_size, "voice.ogg", item.mime_type or "audio/ogg")
    if message.video_note:
        item = message.video_note
        return IncomingFile(item, item.file_size, "video-note.mp4", "video/mp4")
    if message.sticker:
        item = message.sticker
        if item.is_animated:
            name, mime = "sticker.tgs", "application/x-tgsticker"
        elif item.is_video:
            name, mime = "sticker.webm", "video/webm"
        else:
            name, mime = "sticker.webp", "image/webp"
        return IncomingFile(item, item.file_size, name, mime)
    if message.photo:
        item = max(message.photo, key=lambda photo: photo.file_size or 0)
        return IncomingFile(item, item.file_size, "photo.jpg", "image/jpeg")
    return None


def forward_source(message: Message) -> tuple[int, str, str] | None:
    origin = message.forward_origin
    if isinstance(origin, MessageOriginChannel):
        return origin.chat.id, origin.chat.title or str(origin.chat.id), "کانال"
    if isinstance(origin, MessageOriginUser) and origin.sender_user.is_bot:
        bot = origin.sender_user
        title = f"@{bot.username}" if bot.username else bot.full_name or str(bot.id)
        return bot.id, title, "ربات"
    return None


def safe_filename(name: str | None) -> str:
    value = Path(name or "file").name.replace("\x00", "")
    return value[:240] or "file"


def attachment_header(filename: str) -> str:
    from urllib.parse import quote

    ascii_name = filename.encode("ascii", "ignore").decode().replace('"', "") or "download"
    return f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(filename)}'


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


async def create_app(settings: Settings) -> None:
    storage = Storage(settings.data_dir)
    await storage.initialize()
    await storage.ensure_setting("forward_only", "1" if settings.forward_only else "0")

    if settings.telegram_api_base:
        api_server = TelegramAPIServer.from_base(
            settings.telegram_api_base,
            is_local=True,
        )
        if settings.telegram_file_base:
            api_server = TelegramAPIServer(
                base=api_server.base,
                file=settings.telegram_file_base.rstrip("/") + "/file/bot{token}/{path}",
                is_local=True,
            )
        session = AiohttpSession(api=api_server)
    else:
        session = AiohttpSession()
    bot = Bot(token=settings.bot_token, session=session)
    dispatcher = Dispatcher()
    router = Router()
    admins_adding_source: set[int] = set()

    def progress_text(received: int, total: int, started_at: float) -> str:
        elapsed = max(time.monotonic() - started_at, 0.001)
        speed = received / elapsed
        if total > 0:
            percent = min(received * 100 / total, 100)
            filled = min(int(percent / 10), 10)
            bar = "▓" * filled + "░" * (10 - filled)
            amount = f"{human_size(received)} از {human_size(total)}"
            percent_line = f"{percent:.1f}%"
        else:
            bar = "▓░░░░░░░░░"
            amount = human_size(received)
            percent_line = "حجم کل نامشخص"
        return (
            "مرحله ۲ از ۳ — انتقال فایل به سرور\n\n"
            f"{bar}  {percent_line}\n"
            f"دریافت‌شده: {amount}\n"
            f"سرعت: {human_size(int(speed))}/s"
        )

    async def download_with_progress(
        downloadable: Any,
        destination: Path,
        status: Message,
        expected_size: int | None,
    ) -> int:
        preparing_started = time.monotonic()

        async def preparation_updates() -> None:
            while True:
                await asyncio.sleep(10)
                elapsed = int(time.monotonic() - preparing_started)
                minutes, seconds = divmod(elapsed, 60)
                try:
                    await status.edit_text(
                        "مرحله ۱ از ۳ — تلگرام در حال آماده‌سازی فایل است\n\n"
                        f"زمان سپری‌شده: {minutes:02d}:{seconds:02d}\n"
                        "برای فایل‌های چندگیگابایتی این مرحله ممکن است چند دقیقه طول بکشد."
                    )
                except Exception as exc:
                    logger.debug("Could not update preparation message: %s", exc)

        preparation_task = asyncio.create_task(preparation_updates())
        try:
            # Local Bot API downloads the entire file before getFile returns. The
            # aiogram default (60 seconds) is too short for multi-gigabyte media.
            telegram_file = await bot.get_file(
                downloadable.file_id,
                request_timeout=6 * 60 * 60,
            )
        finally:
            preparation_task.cancel()
            with suppress(asyncio.CancelledError):
                await preparation_task
        if not telegram_file.file_path:
            raise RuntimeError("Telegram did not return a file path")

        total = expected_size or telegram_file.file_size or 0
        source_path: Path | None = None
        if settings.telegram_api_base and Path(telegram_file.file_path).is_absolute():
            source_path = Path(telegram_file.file_path)
            if not total:
                total = source_path.stat().st_size

        received = 0
        started_at = time.monotonic()
        last_update = started_at
        last_percent = -1

        async def update_progress(force: bool = False) -> None:
            nonlocal last_update, last_percent
            now = time.monotonic()
            current_percent = int(received * 100 / total) if total else -1
            if not force and (now - last_update < 3 or current_percent == last_percent):
                return
            try:
                await status.edit_text(progress_text(received, total, started_at))
                last_update = now
                last_percent = current_percent
            except Exception as exc:
                # Progress reporting must never interrupt a multi-gigabyte transfer.
                logger.debug("Could not update progress message: %s", exc)

        await update_progress(force=True)
        async with aiofiles.open(destination, "wb") as target:
            if source_path is not None:
                async with aiofiles.open(source_path, "rb") as source:
                    while chunk := await source.read(1024 * 1024):
                        await target.write(chunk)
                        received += len(chunk)
                        await update_progress()
            else:
                file_base = (settings.telegram_file_base or "https://api.telegram.org").rstrip("/")
                url = f"{file_base}/file/bot{settings.bot_token}/{telegram_file.file_path}"
                timeout = aiohttp.ClientTimeout(total=None, connect=60, sock_read=300)
                async with aiohttp.ClientSession(timeout=timeout) as client:
                    async with client.get(url) as response:
                        response.raise_for_status()
                        async for chunk in response.content.iter_chunked(1024 * 1024):
                            await target.write(chunk)
                            received += len(chunk)
                            await update_progress()
        await update_progress(force=True)
        return received

    def is_allowed(message: Message) -> bool:
        if message.from_user and message.from_user.id == settings.admin_user_id:
            return True
        return not settings.allowed_user_ids or bool(
            message.from_user and message.from_user.id in settings.allowed_user_ids
        )

    def is_admin(user_id: int | None) -> bool:
        return bool(settings.admin_user_id and user_id == settings.admin_user_id)

    async def forward_only_enabled() -> bool:
        return await storage.get_setting("forward_only", "0") == "1"

    def admin_keyboard(forward_only: bool) -> InlineKeyboardMarkup:
        toggle_text = (
            "غیرفعال‌کردن محدودیت فوروارد"
            if forward_only
            else "فعال‌کردن محدودیت فوروارد"
        )
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=toggle_text, callback_data="admin:toggle_forward")],
                [InlineKeyboardButton(text="افزودن مبدأ مجاز", callback_data="admin:add_source")],
                [InlineKeyboardButton(text="مدیریت منابع", callback_data="admin:list_sources")],
                [InlineKeyboardButton(text="راهنما", callback_data="admin:help")],
            ]
        )

    @router.message(Command("admin"))
    async def admin_panel(message: Message) -> None:
        if not is_admin(message.from_user.id if message.from_user else None):
            await message.answer("⛔️ دسترسی به پنل مدیریت ندارید.")
            return
        current_mode = await forward_only_enabled()
        mode = "فعال" if current_mode else "غیرفعال"
        await message.answer(
            f"پنل مدیریت ربات\n\nحالت پذیرش فقط از منابع مجاز: {mode}",
            reply_markup=admin_keyboard(current_mode),
        )

    @router.callback_query(F.data == "admin:toggle_forward")
    async def admin_toggle_forward(callback: CallbackQuery) -> None:
        if not is_admin(callback.from_user.id):
            await callback.answer("دسترسی ندارید", show_alert=True)
            return
        enabled = not await forward_only_enabled()
        await storage.set_setting("forward_only", "1" if enabled else "0")
        mode = "فعال شد" if enabled else "غیرفعال شد"
        await callback.answer(f"محدودیت {mode}", show_alert=True)
        if callback.message:
            await callback.message.edit_text(
                f"پنل مدیریت ربات\n\nحالت پذیرش فقط از منابع مجاز: {'فعال' if enabled else 'غیرفعال'}",
                reply_markup=admin_keyboard(enabled),
            )

    @router.callback_query(F.data == "admin:add_source")
    async def admin_add_source(callback: CallbackQuery) -> None:
        if not is_admin(callback.from_user.id):
            await callback.answer("دسترسی ندارید", show_alert=True)
            return
        admins_adding_source.add(callback.from_user.id)
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                "یک فایل یا رسانه را مستقیماً از کانال یا ربات موردنظر برای من فوروارد کنید."
            )

    @router.callback_query(F.data == "admin:help")
    async def admin_help(callback: CallbackQuery) -> None:
        if not is_admin(callback.from_user.id):
            await callback.answer("دسترسی ندارید", show_alert=True)
            return
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                "برای افزودن مبدأ، دکمه «افزودن مبدأ مجاز» را بزنید و یک رسانه را از همان کانال یا ربات فوروارد کنید.\n"
                "پیام‌های Forward Privacy یا محتوای محافظت‌شده قابل شناسایی نیستند."
            )

    @router.callback_query(F.data == "admin:list_sources")
    async def admin_list_sources(callback: CallbackQuery) -> None:
        if not is_admin(callback.from_user.id):
            await callback.answer("دسترسی ندارید", show_alert=True)
            return
        sources = await storage.list_sources()
        await callback.answer()
        if not callback.message:
            return
        if not sources:
            await callback.message.answer("هنوز هیچ مبدأ مجازی ثبت نشده است.")
            return
        buttons = [
            [InlineKeyboardButton(text=f"حذف {title}", callback_data=f"admin:delete:{source_id}")]
            for source_id, title, _ in sources
        ]
        lines = [f"• {title} — {source_type} — {source_id}" for source_id, title, source_type in sources]
        await callback.message.answer(
            "منابع مجاز:\n" + "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )

    @router.callback_query(F.data.startswith("admin:delete:"))
    async def admin_delete_source(callback: CallbackQuery) -> None:
        if not is_admin(callback.from_user.id):
            await callback.answer("دسترسی ندارید", show_alert=True)
            return
        try:
            source_id = int((callback.data or "").rsplit(":", 1)[1])
        except (ValueError, IndexError):
            await callback.answer("درخواست نامعتبر است", show_alert=True)
            return
        await storage.remove_source(source_id)
        await callback.answer("مبدأ حذف شد", show_alert=True)
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=None)

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        if not is_allowed(message):
            await message.answer("⛔️ شما اجازه استفاده از این ربات را ندارید.")
            return
        await message.answer(
            "سلام! فایل یا رسانه موردنظر را برای من بفرستید.\n"
            "فرمت‌های فایل، ویدئو، عکس، صوت، ویس، GIF و استیکر پشتیبانی می‌شوند.\n"
            + (
                f"حداکثر حجم: {settings.max_file_size_mb} مگابایت\n"
                if settings.max_file_size_mb
                else "محدودیت حجم داخلی: ندارد\n"
            )
            +
            f"زمان اعتبار لینک: {settings.file_ttl_hours} ساعت"
        )

    @router.message(
        F.document | F.video | F.audio | F.animation | F.voice | F.video_note | F.sticker | F.photo
    )
    async def receive_media(message: Message) -> None:
        if not is_allowed(message):
            await message.answer("⛔️ شما اجازه استفاده از این ربات را ندارید.")
            return
        media = incoming_file(message)
        if media is None:
            await message.answer("❌ این پیام حاوی فایل قابل دریافت نیست.")
            return
        user_id = message.from_user.id if message.from_user else None
        source = forward_source(message)
        if is_admin(user_id) and user_id in admins_adding_source:
            if source is None:
                await message.answer(
                    "❌ مبدأ قابل شناسایی نیست. رسانه باید مستقیماً از یک کانال یا ربات فوروارد شده باشد."
                )
                return
            source_id, source_title, source_type = source
            await storage.add_source(source_id, source_title, source_type)
            admins_adding_source.discard(user_id)
            await message.answer(
                f"✅ مبدأ مجاز ثبت شد:\n{hbold(source_title)}\nنوع: {source_type}\nشناسه: {hcode(str(source_id))}",
                parse_mode=ParseMode.HTML,
                reply_markup=admin_keyboard(await forward_only_enabled()),
            )
            return
        if await forward_only_enabled():
            if source is None:
                await message.answer("❌ فقط فایل‌های فورواردشده از منابع مجاز پذیرفته می‌شوند.")
                return
            if not await storage.source_is_allowed(source[0]):
                await message.answer("❌ این کانال یا ربات در فهرست منابع مجاز نیست.")
                return
        max_bytes = settings.max_file_size_mb * 1024 * 1024 if settings.max_file_size_mb else 0
        if max_bytes and media.file_size and media.file_size > max_bytes:
            await message.answer(f"❌ حجم فایل بیشتر از {settings.max_file_size_mb} مگابایت است.")
            return

        status = await message.answer("مرحله ۱ از ۳ — بررسی فایل و مبدأ ارسال…")
        token = secrets.token_urlsafe(32)
        stored_name = secrets.token_hex(16)
        destination = storage.path_for(stored_name)
        original_name = safe_filename(media.file_name)
        try:
            actual_size = await download_with_progress(
                media.downloadable,
                destination,
                status,
                media.file_size,
            )
            await status.edit_text(
                "مرحله ۳ از ۳ — انتقال ۱۰۰٪ کامل شد\nدر حال ثبت فایل و ساخت لینک دانلود…"
            )
            if max_bytes and actual_size > max_bytes:
                await storage.delete_stored_file(stored_name)
                await status.edit_text("❌ حجم فایل از محدودیت مجاز بیشتر است.")
                return
            expires_at = int(time.time()) + settings.file_ttl_hours * 3600
            await storage.add(
                StoredFile(
                    token=token,
                    stored_name=stored_name,
                    original_name=original_name,
                    mime_type=media.mime_type or mimetypes.guess_type(original_name)[0] or "application/octet-stream",
                    size=actual_size,
                    expires_at=expires_at,
                )
            )
        except Exception:
            logger.exception("Could not save Telegram file")
            await storage.delete_stored_file(stored_name)
            await status.edit_text("❌ دریافت فایل ناموفق بود. لطفاً دوباره تلاش کنید.")
            return

        link = f"{settings.public_base_url}/d/{token}"
        expiry = datetime.fromtimestamp(expires_at).astimezone().strftime("%Y-%m-%d %H:%M")
        await status.edit_text(
            f"✅ فایل با موفقیت روی سرور ذخیره شد.\n\n"
            f"🔗 {hlink('دانلود مستقیم فایل', link)}\n"
            f"📋 لینک برای کپی:\n{hcode(link)}\n\n"
            f"نام فایل: {hbold(original_name)}\n"
            f"حجم: {human_size(actual_size)}\n"
            f"انقضا: {expiry}",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="⬇️ دانلود فایل", url=link)]
                ]
            ),
            disable_web_page_preview=True,
        )

    @router.message()
    async def unsupported(message: Message) -> None:
        if is_allowed(message):
            await message.answer("لطفاً یک فایل، ویدئو، عکس، صوت، ویس، GIF یا استیکر ارسال کنید.")

    dispatcher.include_router(router)

    def secure_page_headers(response: web.StreamResponse) -> None:
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
            "img-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )

    def expired_response() -> web.Response:
        response = web.Response(
            text=render_expired_page(),
            status=web.HTTPGone.status_code,
            content_type="text/html",
            charset="utf-8",
        )
        secure_page_headers(response)
        return response

    async def get_download_item(request: web.Request) -> tuple[StoredFile, Path] | None:
        item = await storage.get_valid(request.match_info["token"])
        if item is None:
            return None
        path = storage.path_for(item.stored_name)
        if not path.is_file():
            return None
        return item, path

    async def download_page(request: web.Request) -> web.Response:
        result = await get_download_item(request)
        if result is None:
            return expired_response()
        item, _ = result
        response = web.Response(
            text=render_download_page(
                file_name=item.original_name,
                file_size=human_size(item.size),
                mime_type=item.mime_type,
                expires_at=item.expires_at,
                download_url=f"/download/{item.token}",
            ),
            content_type="text/html",
            charset="utf-8",
        )
        secure_page_headers(response)
        return response

    async def download_file(request: web.Request) -> web.StreamResponse:
        result = await get_download_item(request)
        if result is None:
            raise web.HTTPFound(location=f"/d/{request.match_info['token']}")
        item, path = result
        response = web.FileResponse(path)
        response.content_type = item.mime_type
        response.headers["Content-Disposition"] = attachment_header(item.original_name)
        response.headers["Cache-Control"] = "private, no-store"
        return response

    async def health(_: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    web_app = web.Application()
    web_app.router.add_get("/d/{token}", download_page)
    web_app.router.add_get("/download/{token}", download_file)
    web_app.router.add_get("/health", health)
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, settings.host, settings.port)
    await site.start()
    logger.info("Download server listening on %s:%s", settings.host, settings.port)

    async def cleanup_loop() -> None:
        while True:
            try:
                removed = await storage.cleanup_expired()
                if removed:
                    logger.info("Removed %d expired files", removed)
            except Exception:
                logger.exception("Cleanup failed")
            await asyncio.sleep(settings.cleanup_interval_seconds)

    cleanup_task = asyncio.create_task(cleanup_loop())
    try:
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        cleanup_task.cancel()
        with suppress(asyncio.CancelledError):
            await cleanup_task
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(create_app(Settings.from_env()))
