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

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.markdown import hbold, hcode, hlink

from .config import Settings
from .download_page import render_download_page
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

    def is_allowed(message: Message) -> bool:
        return not settings.allowed_user_ids or bool(
            message.from_user and message.from_user.id in settings.allowed_user_ids
        )

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
        max_bytes = settings.max_file_size_mb * 1024 * 1024 if settings.max_file_size_mb else 0
        if max_bytes and media.file_size and media.file_size > max_bytes:
            await message.answer(f"❌ حجم فایل بیشتر از {settings.max_file_size_mb} مگابایت است.")
            return

        status = await message.answer("⏳ در حال دریافت و ساخت لینک…")
        token = secrets.token_urlsafe(32)
        stored_name = secrets.token_hex(16)
        destination = storage.path_for(stored_name)
        original_name = safe_filename(media.file_name)
        try:
            await bot.download(media.downloadable, destination=destination)
            actual_size = destination.stat().st_size
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

    async def get_download_item(request: web.Request) -> tuple[StoredFile, Path]:
        item = await storage.get_valid(request.match_info["token"])
        if item is None:
            raise web.HTTPNotFound(text="Link not found or expired")
        path = storage.path_for(item.stored_name)
        if not path.is_file():
            raise web.HTTPNotFound(text="File not found")
        return item, path

    async def download_page(request: web.Request) -> web.Response:
        item, _ = await get_download_item(request)
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
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
            "img-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        return response

    async def download_file(request: web.Request) -> web.StreamResponse:
        item, path = await get_download_item(request)
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
