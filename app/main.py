from __future__ import annotations

import asyncio
import logging
import mimetypes
import secrets
import time
from contextlib import suppress
from datetime import datetime
from pathlib import Path

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import Message
from aiogram.utils.markdown import hbold, hcode

from .config import Settings
from .storage import Storage, StoredFile

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


def safe_filename(name: str | None) -> str:
    value = Path(name or "file").name.replace("\x00", "")
    return value[:240] or "file"


def attachment_header(filename: str) -> str:
    from urllib.parse import quote

    ascii_name = filename.encode("ascii", "ignore").decode().replace('"', "") or "download"
    return f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(filename)}'


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
            "سلام! فایل را به‌صورت Document برای من بفرستید.\n"
            + (
                f"حداکثر حجم: {settings.max_file_size_mb} مگابایت\n"
                if settings.max_file_size_mb
                else "محدودیت حجم داخلی: ندارد\n"
            )
            +
            f"زمان اعتبار لینک: {settings.file_ttl_hours} ساعت"
        )

    @router.message(F.document)
    async def receive_document(message: Message) -> None:
        if not is_allowed(message):
            await message.answer("⛔️ شما اجازه استفاده از این ربات را ندارید.")
            return
        document = message.document
        assert document is not None
        max_bytes = settings.max_file_size_mb * 1024 * 1024 if settings.max_file_size_mb else 0
        if max_bytes and document.file_size and document.file_size > max_bytes:
            await message.answer(f"❌ حجم فایل بیشتر از {settings.max_file_size_mb} مگابایت است.")
            return

        status = await message.answer("⏳ در حال دریافت و ساخت لینک…")
        token = secrets.token_urlsafe(32)
        stored_name = secrets.token_hex(16)
        destination = storage.path_for(stored_name)
        original_name = safe_filename(document.file_name)
        try:
            await bot.download(document, destination=destination)
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
                    mime_type=document.mime_type or mimetypes.guess_type(original_name)[0] or "application/octet-stream",
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
            f"✅ لینک دانلود آماده است:\n{hcode(link)}\n\n"
            f"نام فایل: {hbold(original_name)}\nانقضا: {expiry}",
            parse_mode=ParseMode.HTML,
        )

    @router.message()
    async def unsupported(message: Message) -> None:
        if is_allowed(message):
            await message.answer("لطفاً فایل را به‌صورت Document ارسال کنید.")

    dispatcher.include_router(router)

    async def download(request: web.Request) -> web.StreamResponse:
        item = await storage.get_valid(request.match_info["token"])
        if item is None:
            raise web.HTTPNotFound(text="Link not found or expired")
        path = storage.path_for(item.stored_name)
        if not path.is_file():
            raise web.HTTPNotFound(text="File not found")
        response = web.FileResponse(path)
        response.content_type = item.mime_type
        response.headers["Content-Disposition"] = attachment_header(item.original_name)
        response.headers["Cache-Control"] = "private, no-store"
        return response

    async def health(_: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    web_app = web.Application()
    web_app.router.add_get("/d/{token}", download)
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
