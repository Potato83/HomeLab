import asyncio
import logging
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import Chat, Message, Update, User

from serverbot.middlewares import AccessMiddleware


@pytest.mark.parametrize(
    ("user_id", "chat_type", "allowed"),
    [(123, "private", True), (456, "private", False), (123, "group", False)],
)
def test_access_is_checked_before_handler(user_id, chat_type, allowed, caplog):
    user = User(id=user_id, is_bot=False, first_name="Test")
    chat = Chat(id=user_id, type=chat_type)
    message = Message(message_id=1, date=datetime.now(UTC), chat=chat, from_user=user, text="/status secret-argument")
    handler = AsyncMock(return_value="handled")
    with caplog.at_level(logging.INFO, logger="serverbot.access"):
        asyncio.run(AccessMiddleware(frozenset({123}))(handler, Update(update_id=1, message=message), {"event_from_user": user, "event_chat": chat}))
    assert handler.await_count == int(allowed)
    assert f"allowed={allowed}" in caplog.text
    assert "secret-argument" not in caplog.text


def test_anonymous_update_is_denied():
    handler = AsyncMock()
    asyncio.run(AccessMiddleware(frozenset({123}))(handler, Update(update_id=1), {}))
    handler.assert_not_awaited()


def test_handler_error_is_logged_without_message_contents(caplog):
    user = User(id=123, is_bot=False, first_name="Test")
    chat = Chat(id=123, type="private")
    handler = AsyncMock(side_effect=RuntimeError("private-secret-data"))
    with caplog.at_level(logging.INFO, logger="serverbot.access"):
        asyncio.run(AccessMiddleware(frozenset({123}))(handler, Update(update_id=1), {"event_from_user": user, "event_chat": chat}))
    assert "RuntimeError" in caplog.text
    assert "private-secret-data" not in caplog.text


def test_dispatcher_supplies_user_to_outer_middleware():
    async def run():
        bot = Bot("123456:" + "A" * 35)
        dp = Dispatcher()
        dp.update.outer_middleware(AccessMiddleware(frozenset({123})))
        handler = AsyncMock()

        @dp.message()
        async def receive(message):
            await handler(message.from_user.id)

        try:
            for user_id in (123, 456):
                user = User(id=user_id, is_bot=False, first_name="Test")
                message = Message(message_id=1, date=datetime.now(UTC), chat=Chat(id=user_id, type="private"), from_user=user, text=" ")
                await dp.feed_update(bot, Update(update_id=user_id, message=message))
            handler.assert_awaited_once_with(123)
        finally:
            await bot.session.close()
    asyncio.run(run())
