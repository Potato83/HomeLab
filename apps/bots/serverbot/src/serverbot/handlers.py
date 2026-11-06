import asyncio
import math
import time
from collections.abc import Callable, Iterable
from html import escape

from aiogram import Router, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiohttp import ClientSession

from serverbot.collectors.backup import BackupReading, get_backup_reading
from serverbot.collectors.battery import BatteryReading, get_battery_reading
from serverbot.collectors.disk import DiskUsageInfo, GetDiskUsage
from serverbot.collectors.http import get_http_status
from serverbot.collectors.system import (
    check_ping,
    get_docker_reading,
    get_external_ip,
    get_system_reading,
    is_process_alive,
    read_log_tail,
)
from serverbot.collectors.temperature import TemperatureReading, get_temperature
from serverbot.config import (
    AppConfig,
    BackupCheckConfig,
    BatteryCheckConfig,
    DiskMountConfig,
    TemperatureCheckConfig,
)
from serverbot.state import low_threshold_status, threshold_status
from serverbot.presentation import DETAIL_PAGES, build_detail_page, build_mobile_status

router = Router()
_last_command_at: dict[tuple[int, str], float] = {}
MAX_MESSAGE_BODY = 3500
MAX_LOG_OUTPUT = 8000


def build_disk_report(
    disks: Iterable[DiskMountConfig],
    usage_reader: Callable[[str], DiskUsageInfo] = GetDiskUsage,
) -> str:
    lines = []
    for disk in disks:
        try:
            info = usage_reader(disk.path)
        except OSError:
            lines.append(f"{disk.path:<24} ERROR: Cannot read disk usage")
            continue
        status = {"ok": "ok", "warning": "!", "critical": "!!!"}[
            threshold_status(info.percent, disk.warning, disk.critical)
        ]
        used_gb = info.used / (1024**3)
        total_gb = info.total / (1024**3)
        lines.append(f"{disk.path:<24} {status:<3} {info.percent:>5.1f}% {used_gb:>7.1f} / {total_gb:>7.1f} GB")
    return "\n".join(lines) or "No disks configured"


def build_disk_details(disks: Iterable[DiskMountConfig]) -> str:
    lines = []
    for disk in disks:
        try:
            info = GetDiskUsage(disk.path)
        except OSError:
            lines.append(f"{disk.path}: ERROR: Cannot read disk usage")
            continue
        lines.append(
            f"{disk.path}\nUsed: {info.used / (1024**3):.1f} GB\n"
            f"Free: {info.free / (1024**3):.1f} GB\nTotal: {info.total / (1024**3):.1f} GB"
        )
    return "Disk details:\n" + ("\n\n".join(lines) if lines else "No disks configured")


def build_temperature_report(
    checks: Iterable[TemperatureCheckConfig],
    temperature_reader: Callable[[TemperatureCheckConfig], TemperatureReading] = get_temperature,
) -> str:
    lines = []
    for check in checks:
        reading = temperature_reader(check)
        if reading.error is not None or reading.value is None:
            lines.append(f"{check.name:<24} ERROR: {reading.error or 'No reading available'}")
            continue
        status = {"ok": "ok", "warning": "!", "critical": "!!!"}[
            threshold_status(reading.value, check.warning, check.critical)
        ]
        lines.append(f"{check.name:<24} {status:<3} {reading.value:>5.1f} °C")
    return "\n".join(lines)


def build_backup_report(checks: Iterable[BackupCheckConfig]) -> str:
    status_labels = {"ok": "ok", "warning": "!", "critical": "!!!", "error": "ERROR"}
    lines = []
    for check in checks:
        reading: BackupReading = get_backup_reading(check)
        if reading.error is not None or reading.last_success_at is None or reading.age_hours is None:
            lines.append(f"{check.name:<24} ERROR: {reading.error or 'Cannot read backup marker'}")
            continue
        last_success = reading.last_success_at.strftime("%Y-%m-%d %H:%M UTC")
        lines.append(
            f"{check.name:<24} {status_labels[reading.status]:<5} "
            f"Last success: {last_success}; age: {reading.age_hours:.1f} h"
        )
    return "\n".join(lines)


def build_battery_report(checks: Iterable[BatteryCheckConfig]) -> str:
    labels = {"ok": "ok", "warning": "!", "critical": "!!!", "error": "ERROR"}
    lines = []
    for check in checks:
        reading: BatteryReading = get_battery_reading(check)
        if reading.battery_error is not None or reading.percentage is None:
            lines.append(f"{check.name} charge  ERROR: {reading.battery_error or 'Cannot read battery capacity'}")
        else:
            status = low_threshold_status(reading.percentage, check.warning_percent, check.critical_percent)
            battery_state = reading.battery_status or "status unavailable"
            lines.append(f"{check.name} charge  {labels[status]:<5} {reading.percentage}% · {battery_state}")
        if reading.charger_error is not None or reading.charger_online is None:
            lines.append(f"{check.name} power   ERROR: {reading.charger_error or 'Cannot read charger status'}")
        elif reading.charger_online:
            lines.append(f"{check.name} power   ok    External power connected")
        else:
            lines.append(f"{check.name} power   !     External power disconnected; battery is discharging")
    return "\n".join(lines)


def _format_uptime(seconds: float) -> str:
    days, remainder = divmod(int(seconds), 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    return f"{days}d {hours}h {minutes}m" if days else f"{hours}h {minutes}m"


def _format_size(value: int) -> str:
    return f"{value / (1024**3):.1f} GB"


def _telegram_units(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


async def build_status_report(config: AppConfig, http_session: ClientSession) -> str:
    sections = ["Disk:\n" + build_disk_report(config.disks)]
    try:
        system = await get_system_reading(config.system.top_processes if config.system is not None else 5)
    except (OSError, ValueError, IndexError):
        sections.append("System:\nCPU/RAM: ERROR: Cannot read system metrics")
    else:
        memory_percent = 100 * system.memory_used_bytes / system.memory_total_bytes if system.memory_total_bytes else 0
        lines = [
            f"CPU: {system.cpu_percent:.1f}%",
            f"Load (1/5/15m): {system.load_average[0]:.2f} / {system.load_average[1]:.2f} / {system.load_average[2]:.2f}",
            f"RAM: {_format_size(system.memory_used_bytes)} used / {_format_size(system.memory_total_bytes)} total "
            f"({memory_percent:.1f}%; {_format_size(system.memory_available_bytes)} available)",
            f"Uptime: {_format_uptime(system.uptime_seconds)}",
            "Top CPU:",
        ]
        lines.extend(
            f"  {process.name} (PID {process.pid}): {process.cpu_percent:.1f}%"
            for process in system.top_cpu
        )
        if not system.top_cpu:
            lines.append("  No process readings")
        lines.append("Top RAM:")
        lines.extend(
            f"  {process.name} (PID {process.pid}): {_format_size(process.memory_bytes)} "
            f"({process.memory_percent:.1f}%)"
            for process in system.top_memory
        )
        sections.append("System:\n" + "\n".join(lines))

    if config.temperatures:
        sections.append("Temperature:\n" + build_temperature_report(config.temperatures))
    if config.batteries:
        sections.append("Battery and power:\n" + build_battery_report(config.batteries))
    if config.backups:
        sections.append("Backups:\n" + build_backup_report(config.backups))
    if config.processes:
        process_lines = []
        for check in config.processes:
            try:
                running = is_process_alive(check.name)
            except OSError:
                state = "ERROR: process list unavailable"
            else:
                state = "running" if running else "NOT RUNNING"
            process_lines.append(f"{check.name}: {state}")
        sections.append("Processes:\n" + "\n".join(process_lines))

    if config.network.external_ip_url is not None or config.network.ping_host is not None:
        if config.network.external_ip_url is not None:
            address = await get_external_ip(config.network.external_ip_url, http_session)
            network_lines = [f"External IP: {address or 'unavailable'}"]
        else:
            network_lines = []
        if config.network.ping_host is not None:
            reachable = await check_ping(config.network.ping_host, config.network.ping_timeout_seconds)
            if reachable is None:
                state = "ping command unavailable"
            else:
                state = "reachable" if reachable else "unreachable"
            network_lines.append(f"Ping {config.network.ping_host}: {state}")
        sections.append("Network:\n" + "\n".join(network_lines))

    if config.host_snapshot is not None:
        from serverbot.collectors.host import read_host_snapshot
        sections.append("Host health:\n" + "\n".join(
            f"{r.name}: {r.status.upper()} · {r.detail}" for r in read_host_snapshot(config.host_snapshot)))

    docker = await get_docker_reading()
    if not docker.available:
        docker_lines = ["Docker CLI not installed"]
    elif docker.error:
        docker_lines = [docker.error]
    elif docker.containers:
        docker_lines = [f"{name}: {status}" for name, status in docker.containers[:20]]
        if len(docker.containers) > 20:
            docker_lines.append(f"… and {len(docker.containers) - 20} more")
    else:
        docker_lines = ["No containers"]
    sections.append("Docker:\n" + "\n".join(docker_lines))

    if config.http_checks:
        lines = []
        for check in config.http_checks:
            reading = await get_http_status(check, http_session)
            if reading.status is None:
                result = f"ERROR: {reading.error}"
            elif reading.ok:
                result = f"HTTP {reading.status}"
            else:
                result = f"HTTP {reading.status}, expected {check.expected_status}"
            lines.append(f"{check.name:<24} {'ok' if reading.ok else '!!!':<3} {result}")
        sections.append("HTTP/API:\n" + "\n".join(lines))
    else:
        sections.append("HTTP/API:\nNo HTTP checks configured")
    return "\n\n".join(sections)


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


def format_single_message(report: str) -> str:
    current = []
    current_length = 0
    truncated = False
    for character in report:
        escaped = escape(character)
        escaped_length = _telegram_units(escaped)
        if current_length + escaped_length > MAX_MESSAGE_BODY:
            truncated = True
            break
        current.append(escaped)
        current_length += escaped_length
    if truncated:
        suffix = escape("\n… summary shortened")
        suffix_length = _telegram_units(suffix)
        while current and current_length + suffix_length > MAX_MESSAGE_BODY:
            current_length -= _telegram_units(current.pop())
        current.extend(suffix)
    return "<pre>" + "".join(current) + "</pre>"


def status_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="↻ Обновить", callback_data="status:refresh"),
        InlineKeyboardButton(text="Подробнее", callback_data="status:details"),
    ]])


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
    await message.answer("Commands:\n/start — show commands\n/status — server summary\n/logs <name> [lines] — configured log tail")


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


@router.callback_query()
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
