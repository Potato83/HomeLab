from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import Message

from . import config
from .database import save_user_data
from .services.restrictions import mute_if_unrestricted

logger = logging.getLogger(__name__)


class AntiSpamMiddleware(BaseMiddleware):
    """Small in-process sliding-window limiter for a single bot replica."""

    def __init__(
        self,
        *,
        limit: int = 7,
        window_seconds: float = 5.0,
        mute_minutes: int = 5,
        profile_refresh_seconds: float = 300.0,
    ) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self.mute_minutes = mute_minutes
        self.profile_refresh_seconds = profile_refresh_seconds
        self._events: dict[tuple[int, int], deque[float]] = defaultdict(deque)
        self._last_profile_save: dict[tuple[int, int], float] = {}
        self._last_cleanup = 0.0

    def _cleanup(self, now: float) -> None:
        if now - self._last_cleanup < 60:
            return
        stale_before = now - max(600, self.profile_refresh_seconds * 2)
        for key in list(self._events):
            events = self._events[key]
            while events and events[0] < now - self.window_seconds:
                events.popleft()
            if not events:
                self._events.pop(key, None)
        for key, timestamp in list(self._last_profile_save.items()):
            if timestamp < stale_before:
                self._last_profile_save.pop(key, None)
        self._last_cleanup = now

    async def _save_profile(self, event: Message, now: float) -> None:
        if not event.from_user:
            return
        key = (event.chat.id, event.from_user.id)
        previous = self._last_profile_save.get(key, 0.0)
        if now - previous < self.profile_refresh_seconds:
            return
        await asyncio.to_thread(
            save_user_data,
            event.chat.id,
            event.from_user.id,
            event.from_user.full_name,
            event.chat.title,
            event.chat.type,
        )
        self._last_profile_save[key] = now

    async def __call__(
        self,
        handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: dict[str, Any],
    ) -> Any:
        if not event.from_user or event.from_user.is_bot:
            return await handler(event, data)

        now = time.monotonic()
        self._cleanup(now)
        await self._save_profile(event, now)

        # The owner, private chats and anonymous sender_chat messages are not
        # moderation targets.
        if (
            event.from_user.id in config.ADMIN_IDS
            or event.chat.type not in {"group", "supergroup"}
            or event.sender_chat is not None
        ):
            return await handler(event, data)

        key = (event.chat.id, event.from_user.id)
        events = self._events[key]
        while events and events[0] <= now - self.window_seconds:
            events.popleft()
        events.append(now)

        if len(events) <= self.limit:
            return await handler(event, data)

        events.clear()
        until_date = datetime.now(timezone.utc) + timedelta(
            minutes=self.mute_minutes
        )
        try:
            if not await mute_if_unrestricted(
                event.bot, event.chat.id, event.from_user.id, until_date
            ):
                return None
            await event.answer(
                f"🔇 Слишком много сообщений. Перерыв {self.mute_minutes} минут."
            )
        except (TelegramBadRequest, TelegramForbiddenError):
            logger.warning(
                "Unable to restrict spammer",
                extra={
                    "chat_id": event.chat.id,
                    "user_id": event.from_user.id,
                },
                exc_info=True,
            )
        return None
