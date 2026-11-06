import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiohttp import ClientSession
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from serverbot.config import AppConfig
from serverbot.presentation import build_mobile_status
from serverbot.monitoring import check_and_notify
from serverbot.state import StateStore

logger = logging.getLogger("serverbot.scheduler")


async def _send_report(bot: Bot, config: AppConfig, http_session: ClientSession,
                       user_ids: frozenset[int], report_name: str) -> None:
    # Build one report and send the same snapshot to each recipient
    try:
        report = await build_mobile_status(config, http_session)
    except Exception as error:  # noqa: BLE001 -- log only the type and keep the scheduled task alive
        logger.error("%s_failed error_type=%s", report_name, type(error).__name__)
        return
    for user_id in sorted(user_ids):
        try:
            await bot.send_message(user_id, report, parse_mode="HTML")
            logger.info("%s_sent user_id=%s", report_name, user_id)
        except (TelegramAPIError, OSError) as error:
            # A blocked bot or failed delivery must not stop other recipients
            logger.warning("%s_send_failed user_id=%s error_type=%s", report_name, user_id, type(error).__name__)


async def send_daily_report(bot: Bot, config: AppConfig, http_session: ClientSession, user_ids: frozenset[int]):
    await _send_report(bot, config, http_session, user_ids, "daily_report")


async def send_periodic_digest(bot: Bot, config: AppConfig, http_session: ClientSession, user_ids: frozenset[int]):
    await _send_report(bot, config, http_session, user_ids, "digest")


def create_scheduler(bot: Bot, config: AppConfig, http_session: ClientSession, user_ids: frozenset[int],
                     state: StateStore | None = None, alert_user_ids: frozenset[int] | None = None) -> AsyncIOScheduler:
    # Use Moscow time regardless of the server's local timezone
    timezone = ZoneInfo("Europe/Moscow")
    scheduler = AsyncIOScheduler(timezone=timezone)
    # Run the daily report at the configured Moscow hour.
    scheduler.add_job(
        send_daily_report, "cron", hour=config.digest_hour, minute=0, second=0, timezone=timezone,
        args=[bot, config, http_session, user_ids], id="daily_report",
        # Avoid overlapping runs; merge missed runs and allow up to 5 minutes of delay
        max_instances=1, coalesce=True, misfire_grace_time=300,
    )
    # A 24-hour digest is the daily report itself, scheduled at digest_hour.
    if config.digest_interval_hours not in (0, 24):
        digest_seconds = config.digest_interval_hours * 3600
        scheduler.add_job(
            send_periodic_digest, "interval", hours=config.digest_interval_hours,
            args=[bot, config, http_session, user_ids], id="digest",
            # Start after one full interval and avoid overlapping or replayed digests
            next_run_time=datetime.now(timezone) + timedelta(seconds=digest_seconds),
            max_instances=1, coalesce=True, misfire_grace_time=min(digest_seconds, 300),
        )
    if state is not None:
        # Check immediately at startup, then repeat independently of scheduled reports
        scheduler.add_job(
            check_and_notify, "interval", seconds=config.check_interval_seconds,
            args=[bot, config, http_session, alert_user_ids if alert_user_ids is not None else user_ids, state],
            id="monitoring", next_run_time=datetime.now(timezone),
            max_instances=1, coalesce=True, misfire_grace_time=min(config.check_interval_seconds, 300),
        )
    return scheduler
