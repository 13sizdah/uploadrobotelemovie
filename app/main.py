from __future__ import annotations

import asyncio
import logging
import mimetypes
import secrets
import shutil
import threading
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
    FSInputFile,
    Message,
    MessageOriginChannel,
    MessageOriginUser,
)
from aiogram.utils.markdown import hbold, hcode, hlink

from .config import Settings
from .admin_web import AdminWeb
from .download_page import render_download_page, render_expired_page
from .object_storage import ObjectStorageManager, UploadCancelled
from .offsite_backup import create_offsite_backup
from .replication_api import ReplicationAPI
from .secure_config import EncryptedConfigStore
from .storage import Storage, StoredFile
from .telegram_cache import validated_cache_path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IncomingFile:
    downloadable: Any
    file_size: int | None
    file_name: str
    mime_type: str


@dataclass(frozen=True)
class PendingUpload:
    requester_id: int
    media: IncomingFile
    max_bytes: int
    created_at: float


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
    await storage.ensure_setting("storage_backend", settings.storage_backend)
    await storage.ensure_setting("replication_count", "1")
    await storage.ensure_setting("replication_paused", "0")
    await storage.ensure_setting("replication_api_token", secrets.token_urlsafe(32))
    await storage.ensure_setting("alert_disk_percent", "90")
    await storage.ensure_setting("alert_queue_count", "10")
    await storage.ensure_setting("offsite_backup_backend", "")
    await storage.ensure_setting(
        "admin_web_password_hash", settings.admin_web_password_hash or ""
    )
    encrypted_config = EncryptedConfigStore(settings.data_dir)
    saved_s3_backends = await encrypted_config.load()
    if saved_s3_backends is None and settings.s3_backends:
        await encrypted_config.save(list(settings.s3_backends))
    active_s3_backends = tuple(saved_s3_backends) if saved_s3_backends is not None else settings.s3_backends
    object_storage = ObjectStorageManager(
        active_s3_backends,
        settings.s3_multipart_chunk_mb,
        settings.s3_presigned_url_seconds,
    )

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
    pending_uploads: dict[str, PendingUpload] = {}
    active_uploads: dict[str, tuple[int, threading.Event]] = {}
    bot_started_at = time.monotonic()

    async def delete_stored_item(item: StoredFile) -> bool:
        try:
            if item.backend_name == "local":
                await storage.delete_stored_file(item.stored_name)
            elif object_storage and item.object_key:
                await object_storage.delete(item.backend_name, item.object_key)
            else:
                raise RuntimeError(f"Storage backend unavailable: {item.backend_name}")
            if object_storage:
                for backend_name, object_key in await storage.replicas_for(item.token):
                    await object_storage.delete(backend_name, object_key)
            await storage.delete_stored_file(item.stored_name)
            await storage.delete_record(item.token)
            return True
        except Exception:
            logger.exception("Could not delete stored item %s", item.token)
            return False

    async def cleanup_expired_items() -> int:
        removed = 0
        for item in await storage.expired_files():
            if await delete_stored_item(item):
                removed += 1
        return removed

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
        cancelled: threading.Event,
        zero_copy_s3: bool = False,
    ) -> tuple[int, Path | None]:
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
        if cancelled.is_set():
            raise UploadCancelled("Upload cancelled by user")

        total = expected_size or telegram_file.file_size or 0
        source_path: Path | None = None
        source_path = validated_cache_path(
            telegram_file.file_path, bool(settings.telegram_api_base)
        )
        if source_path is not None:
            if not total:
                total = source_path.stat().st_size

        # Local Bot API has already materialized the complete file. For S3 mode,
        # return that safe cache path and let boto3 read it directly instead of
        # creating a second multi-gigabyte copy under data/files.
        if zero_copy_s3 and source_path is not None:
            actual_size = source_path.stat().st_size
            if cancelled.is_set():
                raise UploadCancelled("Upload cancelled by user")
            return actual_size, source_path

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
                try:
                    async with aiofiles.open(source_path, "rb") as source:
                        while chunk := await source.read(1024 * 1024):
                            if cancelled.is_set():
                                raise UploadCancelled("Upload cancelled by user")
                            await target.write(chunk)
                            received += len(chunk)
                            await update_progress()
                finally:
                    # Local Bot API keeps a second multi-GB copy indefinitely.
                    # Only unlink paths inside its dedicated cache mount; Telegram
                    # can download the file again later from the original file_id.
                    if validated_cache_path(str(source_path), True) is not None:
                        await asyncio.to_thread(source_path.unlink, missing_ok=True)
            else:
                file_base = (settings.telegram_file_base or "https://api.telegram.org").rstrip("/")
                url = f"{file_base}/file/bot{settings.bot_token}/{telegram_file.file_path}"
                timeout = aiohttp.ClientTimeout(total=None, connect=60, sock_read=300)
                async with aiohttp.ClientSession(timeout=timeout) as client:
                    async with client.get(url) as response:
                        response.raise_for_status()
                        async for chunk in response.content.iter_chunked(1024 * 1024):
                            if cancelled.is_set():
                                raise UploadCancelled("Upload cancelled by user")
                            await target.write(chunk)
                            received += len(chunk)
                            await update_progress()
        await update_progress(force=True)
        return received, None

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
                [
                    InlineKeyboardButton(text="وضعیت سرور", callback_data="admin:status"),
                    InlineKeyboardButton(text="آمار فایل‌ها", callback_data="admin:stats"),
                ],
                [
                    InlineKeyboardButton(text="فایل‌های اخیر", callback_data="admin:files"),
                    InlineKeyboardButton(text="پاک‌سازی منقضی‌ها", callback_data="admin:cleanup"),
                ],
                [InlineKeyboardButton(text="دریافت بکاپ دیتابیس", callback_data="admin:backup")],
                [InlineKeyboardButton(text=toggle_text, callback_data="admin:toggle_forward")],
                [InlineKeyboardButton(text="افزودن مبدأ مجاز", callback_data="admin:add_source")],
                [InlineKeyboardButton(text="مدیریت منابع", callback_data="admin:list_sources")],
                [InlineKeyboardButton(text="راهنما", callback_data="admin:help")],
            ]
        )

    def format_uptime(seconds: int) -> str:
        days, remainder = divmod(seconds, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, _ = divmod(remainder, 60)
        return f"{days} روز، {hours} ساعت، {minutes} دقیقه"

    async def admin_status_text() -> str:
        disk = shutil.disk_usage(settings.data_dir)
        total, active, expired, active_size = await storage.statistics()
        mode = "فعال" if await forward_only_enabled() else "غیرفعال"
        return (
            "وضعیت ربات و فضای ذخیره‌سازی\n\n"
            f"زمان فعالیت ربات: {format_uptime(int(time.monotonic() - bot_started_at))}\n"
            f"فضای آزاد: {human_size(disk.free)} از {human_size(disk.total)}\n"
            f"مصرف فایل‌های فعال: {human_size(active_size)}\n"
            f"فایل فعال: {active} | منقضی در صف حذف: {expired}\n"
            f"مجموع رکوردها: {total}\n"
            f"محدودیت فوروارد: {mode}"
        )

    async def admin_stats_text() -> str:
        total, active, expired, active_size = await storage.statistics()
        sources = await storage.list_sources()
        return (
            "آمار ربات\n\n"
            f"فایل‌های فعال: {active}\n"
            f"فایل‌های منقضی در انتظار پاک‌سازی: {expired}\n"
            f"حجم فایل‌های فعال: {human_size(active_size)}\n"
            f"کل رکوردهای فایل: {total}\n"
            f"منابع مجاز: {len(sources)}"
        )

    async def send_metadata_backup(message: Message) -> None:
        backup_dir = settings.data_dir / "admin-backups"
        backup_path = backup_dir / f"metadata-{int(time.time())}.sqlite3"
        try:
            await storage.create_database_backup(backup_path)
            await message.answer_document(
                FSInputFile(backup_path, filename=backup_path.name),
                caption="بکاپ متادیتای ربات؛ شامل فایل‌های چندگیگابایتی و .env نیست.",
            )
        finally:
            await asyncio.to_thread(backup_path.unlink, missing_ok=True)

    @router.message(Command("status"))
    async def admin_status_command(message: Message) -> None:
        if not is_admin(message.from_user.id if message.from_user else None):
            await message.answer("⛔️ دسترسی ندارید.")
            return
        await message.answer(await admin_status_text())

    @router.message(Command("stats"))
    async def admin_stats_command(message: Message) -> None:
        if not is_admin(message.from_user.id if message.from_user else None):
            await message.answer("⛔️ دسترسی ندارید.")
            return
        await message.answer(await admin_stats_text())

    @router.message(Command("cleanup"))
    async def admin_cleanup_command(message: Message) -> None:
        if not is_admin(message.from_user.id if message.from_user else None):
            await message.answer("⛔️ دسترسی ندارید.")
            return
        removed = await cleanup_expired_items()
        await message.answer(f"پاک‌سازی کامل شد؛ {removed} فایل منقضی حذف شد.")

    @router.message(Command("backup"))
    async def admin_backup_command(message: Message) -> None:
        if not is_admin(message.from_user.id if message.from_user else None):
            await message.answer("⛔️ دسترسی ندارید.")
            return
        await message.answer("در حال ساخت بکاپ متادیتا…")
        await send_metadata_backup(message)

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

    @router.callback_query(F.data == "admin:status")
    async def admin_status(callback: CallbackQuery) -> None:
        if not is_admin(callback.from_user.id):
            await callback.answer("دسترسی ندارید", show_alert=True)
            return
        await callback.answer()
        if callback.message:
            await callback.message.answer(await admin_status_text())

    @router.callback_query(F.data == "admin:stats")
    async def admin_stats(callback: CallbackQuery) -> None:
        if not is_admin(callback.from_user.id):
            await callback.answer("دسترسی ندارید", show_alert=True)
            return
        await callback.answer()
        if callback.message:
            await callback.message.answer(await admin_stats_text())

    @router.callback_query(F.data == "admin:cleanup")
    async def admin_cleanup(callback: CallbackQuery) -> None:
        if not is_admin(callback.from_user.id):
            await callback.answer("دسترسی ندارید", show_alert=True)
            return
        removed = await cleanup_expired_items()
        await callback.answer("پاک‌سازی انجام شد", show_alert=True)
        if callback.message:
            await callback.message.answer(f"پاک‌سازی کامل شد؛ {removed} فایل منقضی حذف شد.")

    @router.callback_query(F.data == "admin:files")
    async def admin_files(callback: CallbackQuery) -> None:
        if not is_admin(callback.from_user.id):
            await callback.answer("دسترسی ندارید", show_alert=True)
            return
        files = await storage.recent_valid_files()
        await callback.answer()
        if not callback.message:
            return
        if not files:
            await callback.message.answer("هیچ فایل فعالی وجود ندارد.")
            return
        lines: list[str] = []
        buttons: list[list[InlineKeyboardButton]] = []
        for index, item in enumerate(files, start=1):
            expiry = datetime.fromtimestamp(item.expires_at).astimezone().strftime("%Y/%m/%d %H:%M")
            lines.append(f"{index}. {item.original_name} — {human_size(item.size)} — {expiry}")
            buttons.append([
                InlineKeyboardButton(
                    text=f"حذف فایل {index}", callback_data=f"admin:file_del:{item.token}"
                )
            ])
        await callback.message.answer(
            "فایل‌های فعال اخیر:\n\n" + "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )

    @router.callback_query(F.data.startswith("admin:file_del:"))
    async def admin_file_delete_prompt(callback: CallbackQuery) -> None:
        if not is_admin(callback.from_user.id):
            await callback.answer("دسترسی ندارید", show_alert=True)
            return
        token = (callback.data or "").removeprefix("admin:file_del:")
        if not token:
            await callback.answer("درخواست نامعتبر است", show_alert=True)
            return
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                "حذف فایل قطعی است و لینک فوراً از کار می‌افتد. ادامه می‌دهید؟",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="تأیید حذف", callback_data=f"admin:file_yes:{token}"),
                    InlineKeyboardButton(text="انصراف", callback_data="admin:cancel"),
                ]]),
            )

    @router.callback_query(F.data.startswith("admin:file_yes:"))
    async def admin_file_delete_confirm(callback: CallbackQuery) -> None:
        if not is_admin(callback.from_user.id):
            await callback.answer("دسترسی ندارید", show_alert=True)
            return
        token = (callback.data or "").removeprefix("admin:file_yes:")
        item = await storage.get(token)
        deleted = bool(item and await delete_stored_item(item))
        await callback.answer("فایل حذف شد" if deleted else "فایل پیدا نشد", show_alert=True)
        if callback.message:
            await callback.message.edit_text("فایل و لینک آن حذف شدند." if deleted else "فایل قبلاً حذف شده بود.")

    @router.callback_query(F.data == "admin:cancel")
    async def admin_cancel(callback: CallbackQuery) -> None:
        if not is_admin(callback.from_user.id):
            await callback.answer("دسترسی ندارید", show_alert=True)
            return
        await callback.answer("لغو شد")
        if callback.message:
            await callback.message.edit_text("عملیات حذف لغو شد.")

    @router.callback_query(F.data == "admin:backup")
    async def admin_backup(callback: CallbackQuery) -> None:
        if not is_admin(callback.from_user.id):
            await callback.answer("دسترسی ندارید", show_alert=True)
            return
        await callback.answer("در حال ساخت بکاپ…")
        if not callback.message:
            return
        await send_metadata_backup(callback.message)

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

    async def perform_upload(
        media: IncomingFile,
        status: Message,
        max_bytes: int,
        cancelled: threading.Event,
        preferred_backend: str | None = None,
    ) -> None:
        token = secrets.token_urlsafe(32)
        stored_name = secrets.token_hex(16)
        destination = storage.path_for(stored_name)
        original_name = safe_filename(media.file_name)
        uploaded_backend: str | None = None
        object_key: str | None = None
        replication_targets: list[tuple[str, str]] = []
        promotion_target: str | None = None
        placement_note = ""
        record_saved = False
        telegram_cache_source: Path | None = None
        try:
            storage_mode = await storage.get_setting("storage_backend", settings.storage_backend)
            actual_size, telegram_cache_source = await download_with_progress(
                media.downloadable,
                destination,
                status,
                media.file_size,
                cancelled,
                zero_copy_s3=storage_mode == "s3",
            )
            if telegram_cache_source is not None:
                await status.edit_text(
                    "مرحله ۱ از ۳ — فایل در cache تلگرام آماده شد\n"
                    "مرحله ۲ — شروع انتقال مستقیم به فضای ابری…"
                )
            else:
                await status.edit_text(
                    "مرحله ۳ از ۳ — انتقال ۱۰۰٪ کامل شد\nدر حال ثبت فایل و ساخت لینک دانلود…"
                )
            if max_bytes and actual_size > max_bytes:
                await storage.delete_stored_file(stored_name)
                await status.edit_text("❌ حجم فایل از محدودیت مجاز بیشتر است.")
                return
            backend_name = "local"
            if storage_mode == "s3":
                if object_storage is None:
                    raise RuntimeError("S3 enabled without configured backends")
                object_key = f"files/{int(time.time())}/{token}/{stored_name}"
                cloud_uploaded = 0
                cloud_started = time.monotonic()

                def cloud_progress(value: int) -> None:
                    nonlocal cloud_uploaded
                    cloud_uploaded = value

                async def cloud_progress_updates() -> None:
                    while True:
                        elapsed = max(time.monotonic() - cloud_started, 0.001)
                        percent = min(cloud_uploaded * 100 / actual_size, 100) if actual_size else 0
                        filled = min(int(percent / 10), 10)
                        try:
                            await status.edit_text(
                                ("مرحله ۲ از ۳ — انتقال مستقیم cache به فضای ابری\n\n"
                                 if telegram_cache_source is not None else
                                 "مرحله ۳ از ۴ — انتقال به فضای ابری\n\n") +
                                f"{'▓' * filled}{'░' * (10 - filled)}  {percent:.1f}%\n"
                                f"ارسال‌شده: {human_size(cloud_uploaded)} از {human_size(actual_size)}\n"
                                f"سرعت: {human_size(int(cloud_uploaded / elapsed))}/s"
                            )
                        except Exception as exc:
                            logger.debug("Could not update S3 progress: %s", exc)
                        await asyncio.sleep(3)

                cloud_task = asyncio.create_task(cloud_progress_updates())
                try:
                    backend_usage = await storage.backend_usage()
                    upload_source = telegram_cache_source or destination
                    upload_preference = preferred_backend
                    if preferred_backend:
                        requested = object_storage.backends[preferred_backend]
                        if requested.role == "replica":
                            transit = object_storage._candidates(
                                actual_size, backend_usage, purpose="primary"
                            )
                            if not transit:
                                raise RuntimeError("No transit backend is available")
                            upload_preference = transit[0].name
                            promotion_target = preferred_backend
                            placement_note = (
                                f"\nمسیر: {upload_preference} (موقت) → "
                                f"Worker ایران → {preferred_backend}"
                            )
                    backend_name = await object_storage.upload(
                        upload_source, object_key, media.mime_type, cloud_progress,
                        backend_usage, cancelled.is_set, upload_preference,
                    )
                    uploaded_backend = backend_name
                finally:
                    cloud_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await cloud_task
                await status.edit_text(
                    (f"مرحله ۳ از ۳ — انتقال مستقیم کامل شد\nفضای انتخاب‌شده: {backend_name}"
                     if telegram_cache_source is not None else
                     f"مرحله ۴ از ۴ — انتقال ابری کامل شد\nفضای انتخاب‌شده: {backend_name}")
                )
                # A user-selected destination is authoritative. Automatic mode
                # retains the administrator's configured replication policy.
                replication_count = (
                    1 if preferred_backend else max(
                        1, int(await storage.get_setting("replication_count", "1"))
                    )
                )
                if promotion_target:
                    replication_targets = [(promotion_target, object_key)]
                    await status.edit_text(
                        "نسخه موقت خارج آماده شد؛ انتقال سریع توسط Worker ایران در صف قرار گرفت…"
                    )
                elif replication_count > 1:
                    await status.edit_text(
                        "نسخه اصلی ثبت شد؛ replicaها در صف پایدار قرار می‌گیرند…"
                    )
                    replication_targets = [
                        (target, object_key)
                        for target in object_storage.replication_targets(
                            backend_name,
                            replication_count,
                            actual_size,
                            backend_usage,
                        )
                    ]
            expires_at = int(time.time()) + settings.file_ttl_hours * 3600
            item = StoredFile(
                token=token,
                stored_name=stored_name,
                original_name=original_name,
                mime_type=media.mime_type or mimetypes.guess_type(original_name)[0] or "application/octet-stream",
                size=actual_size,
                expires_at=expires_at,
                backend_name=backend_name,
                object_key=object_key,
            )
            if backend_name == "local":
                await storage.add(item)
            else:
                await storage.add_with_replication_jobs(
                    item, replication_targets, promote_target=promotion_target
                )
                if not replication_targets:
                    await storage.delete_stored_file(stored_name)
            record_saved = True
        except UploadCancelled:
            await storage.delete_stored_file(stored_name)
            if not record_saved and uploaded_backend and object_key:
                with suppress(Exception):
                    await object_storage.delete(uploaded_backend, object_key)
            await status.edit_text("⛔️ آپلود لغو شد و فایل موقت پاک‌سازی شد.")
            return
        except Exception:
            logger.exception("Could not save Telegram file")
            await storage.delete_stored_file(stored_name)
            if not record_saved and uploaded_backend and object_key:
                with suppress(Exception):
                    await object_storage.delete(uploaded_backend, object_key)
            await status.edit_text("❌ دریافت فایل ناموفق بود. لطفاً دوباره تلاش کنید.")
            return
        finally:
            if telegram_cache_source is not None:
                if validated_cache_path(str(telegram_cache_source), True) is not None:
                    await asyncio.to_thread(telegram_cache_source.unlink, missing_ok=True)

        link = f"{settings.public_base_url}/d/{token}"
        expiry = datetime.fromtimestamp(expires_at).astimezone().strftime("%Y-%m-%d %H:%M")
        await status.edit_text(
            f"✅ فایل با موفقیت روی سرور ذخیره شد.\n\n"
            f"🔗 {hlink('دانلود مستقیم فایل', link)}\n"
            f"📋 لینک برای کپی:\n{hcode(link)}\n\n"
            f"نام فایل: {hbold(original_name)}\n"
            f"حجم: {human_size(actual_size)}\n"
            f"انقضا: {expiry}{placement_note}",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="⬇️ دانلود فایل", url=link)]
                ]
            ),
            disable_web_page_preview=True,
        )

    @router.callback_query(F.data.startswith("upload:dest:"))
    async def confirm_upload(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":", 3)
        if len(parts) != 4:
            await callback.answer("درخواست نامعتبر است", show_alert=True)
            return
        nonce, selected = parts[2], parts[3]
        pending = pending_uploads.get(nonce)
        if pending is None or time.monotonic() - pending.created_at > 600:
            pending_uploads.pop(nonce, None)
            await callback.answer("این درخواست منقضی شده است", show_alert=True)
            if callback.message:
                await callback.message.edit_text("درخواست آپلود منقضی شد؛ فایل را دوباره ارسال کنید.")
            return
        if callback.from_user.id != pending.requester_id:
            await callback.answer("فقط ارسال‌کننده فایل می‌تواند تصمیم بگیرد", show_alert=True)
            return
        preferred_backend = None if selected == "auto" else selected
        if preferred_backend:
            backend = object_storage.backends.get(preferred_backend) if object_storage else None
            if backend is None or backend.role not in {"primary", "replica"}:
                await callback.answer("فضای انتخاب‌شده دیگر در دسترس نیست", show_alert=True)
                return
        pending_uploads.pop(nonce, None)
        cancelled = threading.Event()
        active_uploads[nonce] = (pending.requester_id, cancelled)
        await callback.answer("آپلود آغاز شد")
        if not callback.message:
            return
        abort_keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⛔️ لغو آپلود", callback_data=f"upload:abort:{nonce}")
        ]])
        await callback.message.edit_text(
            "مرحله ۱ از ۳ — بررسی فایل و آماده‌سازی انتقال…",
            reply_markup=abort_keyboard,
        )
        try:
            await perform_upload(
                pending.media, callback.message, pending.max_bytes, cancelled,
                preferred_backend,
            )
        finally:
            active_uploads.pop(nonce, None)

    @router.callback_query(F.data.startswith("upload:abort:"))
    async def abort_active_upload(callback: CallbackQuery) -> None:
        nonce = (callback.data or "").removeprefix("upload:abort:")
        active = active_uploads.get(nonce)
        if active is None:
            await callback.answer("این آپلود دیگر فعال نیست", show_alert=True)
            return
        requester_id, cancelled = active
        if callback.from_user.id != requester_id and not is_admin(callback.from_user.id):
            await callback.answer("فقط ارسال‌کننده یا مدیر می‌تواند لغو کند", show_alert=True)
            return
        cancelled.set()
        await callback.answer("درخواست لغو ثبت شد", show_alert=True)
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=None)

    @router.callback_query(F.data.startswith("upload:cancel:"))
    async def cancel_upload(callback: CallbackQuery) -> None:
        nonce = (callback.data or "").removeprefix("upload:cancel:")
        pending = pending_uploads.get(nonce)
        if pending is None:
            await callback.answer("این درخواست دیگر فعال نیست", show_alert=True)
            return
        if callback.from_user.id != pending.requester_id:
            await callback.answer("فقط ارسال‌کننده فایل می‌تواند تصمیم بگیرد", show_alert=True)
            return
        pending_uploads.pop(nonce, None)
        await callback.answer("لغو شد")
        if callback.message:
            await callback.message.edit_text("آپلود فایل به درخواست شما لغو شد.")

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

        original_name = safe_filename(media.file_name)
        if user_id is None:
            await message.answer("❌ ارسال‌کننده پیام قابل شناسایی نیست.")
            return
        now = time.monotonic()
        for old_nonce, pending in list(pending_uploads.items()):
            if now - pending.created_at > 600:
                pending_uploads.pop(old_nonce, None)
        nonce = secrets.token_urlsafe(8)
        pending_uploads[nonce] = PendingUpload(
            requester_id=user_id,
            media=media,
            max_bytes=max_bytes,
            created_at=now,
        )
        destination_buttons: list[list[InlineKeyboardButton]] = []
        if object_storage is not None:
            for backend in object_storage.backends.values():
                if backend.role not in {"primary", "replica"}:
                    continue
                endpoint = backend.endpoint_url.lower()
                is_iran = endpoint.endswith(".ir") or ".ir/" in endpoint or "parspack" in endpoint
                label = "🇮🇷 ایران" if is_iran else "🌍 خارج"
                destination_buttons.append([
                    InlineKeyboardButton(
                        text=f"{label} — {backend.name}",
                        callback_data=f"upload:dest:{nonce}:{backend.name}",
                    )
                ])
            destination_buttons.append([
                InlineKeyboardButton(
                    text="⚡ انتخاب خودکار و نسخه پشتیبان",
                    callback_data=f"upload:dest:{nonce}:auto",
                )
            ])
        destination_buttons.append([
            InlineKeyboardButton(text="لغو فرایند", callback_data=f"upload:cancel:{nonce}")
        ])
        await message.answer(
            "مرحله ۱ — تأیید آپلود\n\n"
            f"نام فایل: {hbold(original_name)}\n"
            f"حجم: {human_size(media.file_size) if media.file_size else 'نامشخص'}\n\n"
            "فضای ذخیره‌سازی فایل را انتخاب کنید:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=destination_buttons),
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

    async def get_download_item(request: web.Request) -> tuple[StoredFile, Path | None] | None:
        item = await storage.get_valid(request.match_info["token"])
        if item is None:
            return None
        if item.backend_name != "local":
            return item, None
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
        if item.backend_name != "local":
            if object_storage is None or not item.object_key:
                return expired_response()
            locations = [(item.backend_name, item.object_key)]
            locations.extend(await storage.replicas_for(item.token))
            selected = await object_storage.resolve_download_location(locations)
            if selected is None:
                return expired_response()
            backend_name, object_key = selected
            await storage.record_download(backend_name, item.size)
            url = await object_storage.presigned_download(
                backend_name, object_key, item.original_name
            )
            raise web.HTTPFound(location=url)
        assert path is not None
        await storage.record_download("local", item.size)
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
    ReplicationAPI(storage, object_storage).install(web_app)
    active_admin_password_hash = await storage.get_setting(
        "admin_web_password_hash", settings.admin_web_password_hash or ""
    )
    if active_admin_password_hash:
        AdminWeb(
            active_admin_password_hash,
            object_storage,
            encrypted_config,
            storage,
        ).install(web_app)
    else:
        async def admin_not_configured(_: web.Request) -> web.Response:
            return web.Response(
                text=(
                    "پنل مدیریت فعال نشده است. متغیر ADMIN_WEB_PASSWORD_HASH را "
                    "در فایل .env تنظیم و کانتینر file-link-bot را بازسازی کنید."
                ),
                status=503,
                content_type="text/plain",
                charset="utf-8",
                headers={"Cache-Control": "no-store"},
            )

        web_app.router.add_get("/manage", admin_not_configured)
        web_app.router.add_get("/manage/", admin_not_configured)
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, settings.host, settings.port)
    await site.start()
    logger.info("Download server listening on %s:%s", settings.host, settings.port)

    async def cleanup_loop() -> None:
        while True:
            try:
                removed = await cleanup_expired_items()
                if removed:
                    logger.info("Removed %d expired files", removed)
                orphaned = await storage.cleanup_orphan_files(min_age_seconds=3600)
                if orphaned:
                    logger.info("Removed %d orphaned temporary files", orphaned)
            except Exception:
                logger.exception("Cleanup failed")
            await asyncio.sleep(settings.cleanup_interval_seconds)

    async def storage_health_loop() -> None:
        while True:
            try:
                if object_storage.backends:
                    await object_storage.health_check_all()
            except Exception:
                logger.exception("S3 health check cycle failed")
            await asyncio.sleep(60)

    async def replication_loop() -> None:
        while True:
            try:
                if await storage.get_setting("replication_paused", "0") == "1":
                    await asyncio.sleep(5)
                    continue
                external_targets = await storage.active_worker_targets()
                jobs = await storage.due_replication_jobs(
                    limit=5, excluded_targets=external_targets
                )
                if not jobs:
                    await asyncio.sleep(5)
                    continue
                for job in jobs:
                    item = await storage.get(job.token)
                    if item is None or not item.object_key:
                        continue
                    source = storage.path_for(item.stored_name)
                    try:
                        if not source.is_file():
                            await object_storage.download_to(
                                item.backend_name, item.object_key, source
                            )
                        await object_storage.upload_to(
                            job.target_backend, source, job.object_key, item.mime_type
                        )
                        completion = await storage.complete_replication_job(job)
                        if (
                            completion.promoted
                            and completion.old_backend
                            and completion.old_object_key
                        ):
                            with suppress(Exception):
                                await object_storage.delete(
                                    completion.old_backend, completion.old_object_key
                                )
                        logger.info(
                            "Replication completed for %s to %s",
                            job.token,
                            job.target_backend,
                        )
                        if await storage.pending_replication_count(job.token) == 0:
                            await storage.delete_stored_file(item.stored_name)
                    except Exception as exc:
                        logger.exception(
                            "Replication job %s failed for backend %s",
                            job.id,
                            job.target_backend,
                        )
                        await storage.fail_replication_job(job, type(exc).__name__)
            except Exception:
                logger.exception("Replication worker cycle failed")
                await asyncio.sleep(5)

    async def automatic_backup_loop() -> None:
        while True:
            try:
                backup_dir = storage.data_dir / "backups"
                filename = f"auto-{datetime.now().astimezone().strftime('%Y%m%d-%H%M%S')}.sqlite3"
                await storage.create_database_backup(backup_dir / filename)
                backups = sorted(backup_dir.glob("auto-*.sqlite3"), reverse=True)
                for old in backups[7:]:
                    await asyncio.to_thread(old.unlink, missing_ok=True)
                logger.info("Automatic database backup created: %s", filename)
                offsite_backend = await storage.get_setting("offsite_backup_backend", "")
                if offsite_backend:
                    object_key = await create_offsite_backup(
                        storage, encrypted_config, object_storage, offsite_backend
                    )
                    logger.info("Offsite backup uploaded: %s", object_key)
            except Exception:
                logger.exception("Automatic database backup failed")
            await asyncio.sleep(24 * 3600)

    async def alert_loop() -> None:
        disk_alerted = False
        storage_alerted = False
        queue_alerted = False
        while True:
            try:
                disk = shutil.disk_usage(storage.data_dir)
                disk_threshold = int(await storage.get_setting("alert_disk_percent", "90"))
                queue_threshold = int(await storage.get_setting("alert_queue_count", "10"))
                pending_jobs = await storage.pending_replication_count()
                disk_high = disk.used * 100 / disk.total >= disk_threshold
                queue_high = pending_jobs >= queue_threshold
                unhealthy = [
                    item.name for item in object_storage.backends.values()
                    if item.enabled and item.unhealthy_until > time.monotonic()
                ]
                if settings.admin_user_id and disk_high and not disk_alerted:
                    await bot.send_message(
                        settings.admin_user_id,
                        f"⚠️ هشدار دیسک: {disk.used * 100 / disk.total:.1f}% مصرف شده است.",
                    )
                if settings.admin_user_id and unhealthy and not storage_alerted:
                    await bot.send_message(
                        settings.admin_user_id,
                        "⚠️ فضای ذخیره‌سازی ناسالم: " + ", ".join(unhealthy),
                    )
                if settings.admin_user_id and queue_high and not queue_alerted:
                    await bot.send_message(
                        settings.admin_user_id,
                        f"⚠️ هشدار صف انتقال: {pending_jobs} کار در انتظار است.",
                    )
                disk_alerted = disk_high
                storage_alerted = bool(unhealthy)
                queue_alerted = queue_high
            except Exception:
                logger.exception("Operational alert cycle failed")
            await asyncio.sleep(300)

    cleanup_task = asyncio.create_task(cleanup_loop())
    health_task = asyncio.create_task(storage_health_loop())
    replication_task = asyncio.create_task(replication_loop())
    backup_task = asyncio.create_task(automatic_backup_loop())
    alert_task = asyncio.create_task(alert_loop())
    try:
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        cleanup_task.cancel()
        health_task.cancel()
        replication_task.cancel()
        backup_task.cancel()
        alert_task.cancel()
        with suppress(asyncio.CancelledError):
            await cleanup_task
        with suppress(asyncio.CancelledError):
            await health_task
        with suppress(asyncio.CancelledError):
            await replication_task
        with suppress(asyncio.CancelledError):
            await backup_task
        with suppress(asyncio.CancelledError):
            await alert_task
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(create_app(Settings.from_env()))
