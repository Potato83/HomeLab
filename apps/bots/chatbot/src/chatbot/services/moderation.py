from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from .restrictions import mute_if_unrestricted

from .economy import (
    SleepCharge,
    get_sleep_action,
    latest_sleep_deadline,
    mark_sleep_applied,
    pending_sleep_actions,
    refund_sleep,
)

logger = logging.getLogger(__name__)
# The database lock enforces one bot process; serialize handler and worker calls.
SLEEP_ACTION_LOCK = asyncio.Lock()
SLEEP_WORKER_INTERVAL_SECONDS = 30
SLEEP_ACTION_GRACE_SECONDS = 10


async def _apply_sleep(
    bot: Bot,
    chat_id: int,
    attacker_id: int,
    target_id: int,
    charge: SleepCharge,
) -> None:
    current = await asyncio.to_thread(get_sleep_action, charge.operation_key)
    if current is None or current.status != "pending":
        return
    charge = current

    if charge.until_date <= int(time.time()):
        # The request may have reached Telegram before the previous process
        # stopped. Keep the charge when its outcome can no longer be checked.
        logger.warning(
            "Sleep action expired before reconciliation; keeping its charge",
            extra={"chat_id": chat_id, "target_id": target_id},
        )
        await asyncio.to_thread(mark_sleep_applied, charge)
        return

    latest_deadline = await asyncio.to_thread(
        latest_sleep_deadline,
        chat_id,
        target_id,
    )
    until_date = max(charge.until_date, latest_deadline or 0)

    try:
        # An ambiguous earlier request may already have succeeded. If the
        # member is now restricted, preserve it and finish without replaying.
        await mute_if_unrestricted(
            bot, chat_id, target_id, datetime.fromtimestamp(until_date, timezone.utc)
        )
    except (TelegramBadRequest, TelegramForbiddenError):
        logger.warning(
            "Sleep moderation was rejected; refunding the charge",
            extra={"chat_id": chat_id, "target_id": target_id},
            exc_info=True,
        )
        await asyncio.to_thread(refund_sleep, chat_id, attacker_id, charge)
        return

    await asyncio.to_thread(mark_sleep_applied, charge)


async def run_sleep_worker(bot: Bot) -> None:
    while True:
        try:
            actions = await asyncio.to_thread(
                pending_sleep_actions,
                older_than_seconds=SLEEP_ACTION_GRACE_SECONDS,
            )
            for chat_id, attacker_id, target_id, charge in actions:
                try:
                    async with SLEEP_ACTION_LOCK:
                        await _apply_sleep(
                            bot,
                            chat_id,
                            attacker_id,
                            target_id,
                            charge,
                        )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "Failed to reconcile pending sleep action",
                        extra={"chat_id": chat_id, "target_id": target_id},
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Sleep reconciliation worker iteration failed")

        await asyncio.sleep(SLEEP_WORKER_INTERVAL_SECONDS)
