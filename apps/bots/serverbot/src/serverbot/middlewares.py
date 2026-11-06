import logging

from aiogram import BaseMiddleware
from aiogram.types import Update

logger = logging.getLogger("serverbot.access")


class AccessMiddleware(BaseMiddleware):
    def __init__(self, allowed_user_ids: frozenset[int]):
        self.allowed_user_ids = allowed_user_ids

    async def __call__(self, handler, event: Update, data):
        # Aiogram adds the sender and chat to data before this middleware runs
        user = data.get("event_from_user")
        chat = data.get("event_chat")
        # Private chats keep reports from being shared with other group members
        allowed = user is not None and user.id in self.allowed_user_ids and chat is not None and chat.type == "private"
        action = "update"
        if event.callback_query:
            action = "callback_query"
        message = event.message or event.edited_message
        if message:
            action = "message"
            parts = (message.text or "").split(maxsplit=1)
            if parts:
                command = parts[0].split("@", 1)[0]
                if command in ("/start", "/status"):
                    action = command
        # Never log message text, usernames, callback payloads or full updates
        logger.info("access user_id=%s chat_id=%s action=%s allowed=%s",
                    user.id if user else None, chat.id if chat else None, action, allowed)
        if not allowed:
            # Stop here: unauthorized updates never reach the handler
            return None
        try:
            # Pass authorized updates to the normal command handling
            return await handler(event, data)
        except Exception as error:  # noqa: BLE001 -- log only the type to keep update data and secrets out of logs
            logger.error("handler_failed user_id=%s error_type=%s", user.id, type(error).__name__)
            return None
