import asyncio
import math
import time
from html import escape

from aiogram import Router, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiohttp import ClientSession

from serverbot.collectors.system import read_log_tail
from serverbot.config import AppConfig
from serverbot.presentation import DETAIL_PAGES, build_detail_page, build_mobile_status

router = Router()
_last_command_at: dict[tuple[int, str], float] = {}
MAX_MESSAGE_BODY = 3500
MAX_LOG_OUTPUT = 8000


def _telegram_units(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def format_report_messages(report: str) -> list[str]:
    chunks = []
    current = []
    current_length = 0
    for character in report:
        escaped = escape(character)
        escaped_length = _telegram_units(escaped)
        if current_length + escaped_length > MAX_MESSAGE_BODY:
            chunks.append("<pre>" + "".join(current) + "</pre>")
            current = []
            current_length = 0
        current.append(escaped)
        current_length += escaped_length
    if current or not chunks:
        chunks.append("<pre>" + "".join(current) + "</pre>")
    return chunks


def status_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="↻ Обновить", callback_data="status:refresh"),
        InlineKeyboardButton(text="Подробнее", callback_data="status:details"),
    ], [InlineKeyboardButton(text="⚙️ Управление", callback_data="control:home")]])


def _rate_limit_remaining(user_id: int, command: str, cooldown: int) -> int:
    now = time.monotonic()
    key = (user_id, command)
    previous = _last_command_at.get(key)
    if previous is not None and now - previous < cooldown:
        return math.ceil(cooldown - (now - previous))
    if len(_last_command_at) > 5000:
        _last_command_at.clear()
    _last_command_at[key] = now
    return 0


@router.message(Command("start"))
async def command_start_handler(message: types.Message) -> None:
    await message.answer("/status — сводка по серверу\n/panel — управление VM и ботами\n/logs <имя> [строк] — разрешённый журнал")


@router.message(Command("status"))
async def command_status_handler(message: types.Message, config: AppConfig, http_session: ClientSession) -> None:
    parts = (message.text or "").split()
    command = parts[0].split("@", 1)[0] if parts else ""
    if command != "/status" or len(parts) != 1:
        await message.answer("Usage: /status")
        return
    remaining = _rate_limit_remaining(message.from_user.id, "status", config.command_rate_limit_seconds)
    if remaining:
        await message.answer(f"Please wait {remaining}s before refreshing status.")
        return
    report = await build_mobile_status(config, http_session)
    await message.answer(report, parse_mode="HTML", reply_markup=status_keyboard())


def detail_keyboard(page: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=("• " if page == key else "") + DETAIL_PAGES[key],
                              callback_data="status:page:" + key) for key in pair]
        for pair in (("resources", "bots"), ("network", "storage"))
    ] + [[InlineKeyboardButton(text="← Сводка", callback_data="status:summary"),
          InlineKeyboardButton(text="↻ Обновить", callback_data="status:page:" + page)]])


async def _edit_status(message, report, keyboard):
    try:
        await message.edit_text(report, parse_mode="HTML", reply_markup=keyboard)
    except TelegramBadRequest as error:
        if "message is not modified" not in str(error).lower():
            raise


async def is_status_callback(callback: types.CallbackQuery) -> bool:
    return (callback.data or '').startswith('status:')


@router.callback_query(is_status_callback)
async def status_callback_handler(
    callback: types.CallbackQuery,
    config: AppConfig,
    http_session: ClientSession,
) -> None:
    pages = {"status:page:" + key: key for key in DETAIL_PAGES}
    if callback.data not in {"status:refresh", "status:details", "status:summary", *pages}:
        await callback.answer()
        return
    message = callback.message
    if not isinstance(message, types.Message):
        await callback.answer()
        return
    if callback.data in {"status:refresh", "status:summary"}:
        cooldown = config.command_rate_limit_seconds if callback.data == "status:refresh" else 1
        key = "status" if callback.data == "status:refresh" else "summary-nav"
        remaining = _rate_limit_remaining(callback.from_user.id, key, cooldown)
        if remaining:
            await callback.answer(f"Повторите через {remaining} с")
            return
        report = await build_mobile_status(config, http_session)
        await _edit_status(message, report, status_keyboard())
    else:
        remaining = _rate_limit_remaining(callback.from_user.id, "details-nav", 1)
        if remaining:
            await callback.answer(f"Повторите через {remaining} с")
            return
        page = pages.get(callback.data, "resources")
        report = await build_detail_page(config, http_session, page)
        await _edit_status(message, report, detail_keyboard(page))
    await callback.answer()


@router.message(Command("logs"))
async def command_logs_handler(message: types.Message, config: AppConfig) -> None:
    parts = (message.text or "").split()
    command = parts[0].split("@", 1)[0] if parts else ""
    if command != "/logs" or len(parts) not in (1, 2, 3):
        await message.answer("Usage: /logs <name> [lines]")
        return
    if len(parts) == 1:
        names = ", ".join(file.name for file in config.log_files) or "none configured"
        await message.answer(f"Usage: /logs <name> [lines]\nConfigured logs: {names}")
        return
    file = next((item for item in config.log_files if item.name == parts[1]), None)
    if file is None:
        await message.answer("Unknown log name. Use /logs to list configured logs.")
        return
    line_count = 50
    if len(parts) == 3:
        if len(parts[2]) > 3 or not parts[2].isascii() or not parts[2].isdigit():
            await message.answer("Line count must be a number.")
            return
        line_count = int(parts[2])
    if not 1 <= line_count <= config.max_log_lines:
        await message.answer(f"Line count must be from 1 to {config.max_log_lines}.")
        return
    remaining = _rate_limit_remaining(message.from_user.id, "logs", config.command_rate_limit_seconds)
    if remaining:
        await message.answer(f"Please wait {remaining}s before reading logs again.")
        return
    try:
        lines = await asyncio.to_thread(read_log_tail, file.path, config.log_directories, line_count)
    except (OSError, RuntimeError, ValueError):
        report = f"{file.name}: Cannot read configured log file"
    else:
        lines = ["".join(char if char.isprintable() or char == "\t" else "?" for char in line) for line in lines]
        report = f"{file.name}: last {line_count} lines\n" + ("\n".join(lines) or "(empty file)")
        if len(report) > MAX_LOG_OUTPUT:
            marker = "(log output truncated)\n"
            report = marker + report[-(MAX_LOG_OUTPUT - len(marker)):]
    for text in format_report_messages(report):
        await message.answer(text, parse_mode="HTML")
