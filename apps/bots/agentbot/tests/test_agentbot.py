import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.types import Chat, Message, Update, User

from agentbot.main import dispatcher
from agentbot.service import Service
from agentbot.store import Store


def reply(mid, text='answer', **extra):
    return {'info': {'id': 'msg_reply', 'role': 'assistant', 'parentID': mid,
                     'time': {'completed': 1}, 'finish': 'stop', **extra},
            'parts': [{'type': 'text', 'text': text}]}


@pytest.mark.parametrize(('uid', 'kind', 'allowed'), [(123, 'private', True), (456, 'private', False), (123, 'group', False)])
def test_real_dispatcher_authorization(tmp_path, uid, kind, allowed):
    async def run():
        store = Store(tmp_path / 's.db')
        backend = AsyncMock()
        backend.create.return_value = 'ses_new'
        dp = dispatcher(store, backend, 123)
        bot = Bot('123456:' + 'A' * 35)
        bot.session.make_request = AsyncMock(return_value=True)
        try:
            message = Message(message_id=1, date=datetime.now(UTC), chat=Chat(id=uid, type=kind),
                              from_user=User(id=uid, is_bot=False, first_name='Test'), text='/new')
            await dp.feed_update(bot, Update(update_id=1, message=message))
            assert backend.create.await_count == int(allowed)
            assert bot.session.make_request.await_count == int(allowed)
            assert store.current() == ('ses_new' if allowed else None)
        finally:
            await bot.session.close()
            store.close()
    asyncio.run(run())


def test_telegram_task_through_backend_to_answer(tmp_path):
    async def run():
        store = Store(tmp_path / 's.db')
        store.add_session('ses_a')
        backend = AsyncMock()
        backend.statuses.return_value = {}
        bot = Bot('123456:' + 'A' * 35)
        bot.session.make_request = AsyncMock(return_value=True)
        try:
            dp = dispatcher(store, backend, 123)
            message = Message(message_id=1, date=datetime.now(UTC), chat=Chat(id=123, type='private'),
                              from_user=User(id=123, is_bot=False, first_name='Test'), text='What is 2+2?')
            update = Update(update_id=42, message=message)
            await dp.feed_update(bot, update)
            send = AsyncMock()
            service = Service(store, backend, send)
            await service.tick()
            mid = store.get(42)['message']
            backend.submit.assert_awaited_once_with('ses_a', mid, 'What is 2+2?')
            backend.messages.return_value = [reply(mid, '4')]
            await service.tick()
            assert store.get(42)['state'] == 'done'
            assert send.call_args.args[0].endswith('4')
            await dp.feed_update(bot, update)
            backend.submit.assert_awaited_once()
        finally:
            await bot.session.close()
            store.close()
    asyncio.run(run())
