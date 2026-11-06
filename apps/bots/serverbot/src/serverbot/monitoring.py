import logging
from dataclasses import dataclass

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiohttp import ClientSession

from serverbot.collectors.backup import get_backup_reading
from serverbot.collectors.battery import get_battery_reading
from serverbot.collectors.disk import GetDiskUsage
from serverbot.collectors.http import get_http_status
from serverbot.collectors.system import get_system_reading, is_process_alive
from serverbot.collectors.temperature import get_temperature
from serverbot.config import AppConfig
from serverbot.handlers import format_report_messages
from serverbot.state import StateError, StateStore, low_threshold_status, should_notify, threshold_status

logger = logging.getLogger("serverbot.monitoring")


@dataclass(frozen=True)
class CheckResult:
    key: str
    name: str
    status: str
    detail: str


async def collect_checks(config: AppConfig, http_session: ClientSession) -> list[CheckResult]:
    results = []
    if config.system is not None:
        try:
            system = await get_system_reading(config.system.top_processes)
        except (OSError, ValueError, IndexError):
            results.extend((
                CheckResult("system:cpu", "CPU usage", "error", "Cannot read CPU usage"),
                CheckResult("system:memory", "Memory usage", "error", "Cannot read memory usage"),
            ))
        else:
            cpu_status = threshold_status(
                system.cpu_percent, config.system.cpu_warning_percent, config.system.cpu_critical_percent,
            )
            memory_percent = 100 * system.memory_used_bytes / system.memory_total_bytes if system.memory_total_bytes else 0
            memory_status = threshold_status(
                memory_percent, config.system.memory_warning_percent, config.system.memory_critical_percent,
            )
            results.append(CheckResult("system:cpu", "CPU usage", cpu_status, f"CPU: {system.cpu_percent:.1f}%"))
            results.append(CheckResult(
                "system:memory", "Memory usage", memory_status, f"RAM: {memory_percent:.1f}% used",
            ))
    for disk in config.disks:
        try:
            reading = GetDiskUsage(disk.path)
            status = threshold_status(reading.percent, disk.warning, disk.critical)
            detail = f"Used: {reading.percent:.1f}%"
        except OSError:
            status, detail = "error", "Cannot read disk usage"
        results.append(CheckResult(f"disk:{disk.path}", f"Disk {disk.path}", status, detail))
    for check in config.temperatures:
        reading = get_temperature(check)
        if reading.error is not None or reading.value is None:
            status, detail = "error", "Cannot read temperature"
        else:
            status = threshold_status(reading.value, check.warning, check.critical)
            detail = f"Temperature: {reading.value:.1f} °C"
        results.append(CheckResult(f"temperature:{check.name}", check.name, status, detail))
    for check in config.backups:
        reading = get_backup_reading(check)
        if reading.error is not None or reading.age_hours is None or reading.last_success_at is None:
            status, detail = "error", reading.error or "Cannot read backup marker"
        else:
            status = reading.status
            timestamp = reading.last_success_at.strftime("%Y-%m-%d %H:%M UTC")
            detail = f"Last success: {timestamp}; age: {reading.age_hours:.1f} h"
        results.append(CheckResult(f"backup:{check.name}", check.name, status, detail))
    for check in config.batteries:
        reading = get_battery_reading(check)
        if reading.battery_error is not None or reading.percentage is None:
            status, detail = "error", reading.battery_error or "Cannot read battery capacity"
        else:
            status = low_threshold_status(reading.percentage, check.warning_percent, check.critical_percent)
            battery_state = reading.battery_status or "status unavailable"
            detail = f"Battery: {reading.percentage}% · {battery_state}"
        results.append(CheckResult(f"battery:{check.name}:charge", f"{check.name} charge", status, detail))

        if reading.charger_error is not None or reading.charger_online is None:
            status, detail = "error", reading.charger_error or "Cannot read charger status"
        elif reading.charger_online:
            status = "ok"
            detail = "External power connected"
        else:
            status = "warning"
            detail = "External power disconnected; battery is discharging"
        if reading.percentage is not None:
            detail += f" · battery {reading.percentage}%"
        results.append(CheckResult(f"battery:{check.name}:charger", f"{check.name} power", status, detail))
    for check in config.processes:
        try:
            alive = is_process_alive(check.name)
        except OSError:
            status, detail = "error", "Cannot read process list"
        else:
            status = "ok" if alive else "critical"
            detail = "Process is running" if alive else "Process is not running"
        results.append(CheckResult(f"process:{check.name}", check.name, status, detail))
    for check in config.http_checks:
        reading = await get_http_status(check, http_session)
        if reading.status is None:
            status, detail = "error", reading.error or "HTTP request failed"
        else:
            status = "ok" if reading.ok else "critical"
            detail = f"HTTP {reading.status}, expected {check.expected_status}"
        results.append(CheckResult(f"http:{check.name}", check.name, status, detail))
    if config.host_snapshot is not None:
        from serverbot.collectors.host import read_host_snapshot
        results.extend(CheckResult(r.key, r.name, r.status, r.detail) for r in read_host_snapshot(config.host_snapshot))
    return results


async def check_and_notify(bot: Bot, config: AppConfig, http_session: ClientSession,
                           user_ids: frozenset[int], state: StateStore) -> None:
    try:
        results = await collect_checks(config, http_session)
    except Exception as error:  # noqa: BLE001 -- log only the type, keep the periodic job alive
        logger.error("monitoring_failed error_type=%s", type(error).__name__)
        return
    # Remember delivery separately for each recipient; prune removed users/checks
    current_keys = {result.key for result in results}
    new_states = {
        str(user_id): {key: value for key, value in state.states.get(str(user_id), {}).items() if key in current_keys}
        for user_id in sorted(user_ids)
    }
    for user_id in sorted(user_ids):
        saved = new_states[str(user_id)]
        for result in results:
            previous = saved.get(result.key)
            if should_notify(previous, result.status):
                title = "RECOVERED" if result.status == "ok" else "ALERT"
                message = f"{title}: {result.name}\n{(previous or 'unknown').upper()} -> {result.status.upper()}\n{result.detail}"
                try:
                    for text in format_report_messages(message):
                        await bot.send_message(user_id, text, parse_mode="HTML")
                except (TelegramAPIError, OSError) as error:
                    logger.warning("alert_send_failed user_id=%s error_type=%s", user_id, type(error).__name__)
                    # Keep the previous state so the next poll can retry delivery
                    continue
                logger.info("state_changed user_id=%s state=%s", user_id, result.status)
            saved[result.key] = result.status
    if new_states != state.states:
        try:
            state.save(new_states)
        except StateError:
            logger.error("monitoring_state_save_failed")
