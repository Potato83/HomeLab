"""Regression: games and anti-spam cannot replace administrator restrictions."""
import asyncio
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from chatbot import database, middlewares
from chatbot.handlers.shop import _process_sleep_action
from chatbot.middlewares import AntiSpamMiddleware
from chatbot.services.economy import charge_sleep, get_balance, get_sleep_action
from chatbot.services.moderation import _apply_sleep
from chatbot.services.restrictions import mute_if_unrestricted

def member(status='member', until=0, **permissions):
    return SimpleNamespace(status=status, until_date=until, **permissions)

def bot_for(*members):
    return SimpleNamespace(get_chat_member=AsyncMock(side_effect=list(members)),
                           restrict_chat_member=AsyncMock())

def message(bot):
    return SimpleNamespace(bot=bot, chat=SimpleNamespace(id=-100, type='supergroup', title='Test'),
        message_id=88, answer=AsyncMock(), sender_chat=None,
        from_user=SimpleNamespace(id=1, is_bot=False, full_name='A'),
        reply_to_message=SimpleNamespace(from_user=SimpleNamespace(id=2, full_name='B')))

@pytest.mark.parametrize('policy', [
    member('restricted', 0, can_send_messages=False),
    member('restricted', 0, can_send_messages=True, can_send_photos=False),
    member('restricted', int(time.time()) + 86400, can_send_messages=False),
    member('administrator'), member('creator'), member('left'), member('kicked'),
])
def test_existing_policy_never_replaced(policy):
    bot = bot_for(policy)
    assert not asyncio.run(mute_if_unrestricted(bot, -100, 2, datetime.now(timezone.utc)+timedelta(minutes=2)))
    bot.restrict_chat_member.assert_not_awaited()

def test_ordinary_member_can_be_muted():
    bot = bot_for(member())
    deadline = datetime.now(timezone.utc) + timedelta(minutes=2)
    assert asyncio.run(mute_if_unrestricted(bot, -100, 2, deadline))
    assert bot.restrict_chat_member.await_args.kwargs['until_date'] == deadline

def test_sleep_preserves_policy_without_charge(funded_db, monkeypatch):
    monkeypatch.setattr(database, 'DEFAULT_DB_PATH', funded_db)
    bot = bot_for(member('restricted', 0))
    asyncio.run(_process_sleep_action(message(bot), 1, 2))
    assert get_balance(-100, 1) == 100
    assert get_sleep_action('sleep:-100:88') is None
    bot.restrict_chat_member.assert_not_awaited()

def test_policy_change_before_write_refunds_new_charge(funded_db, monkeypatch):
    monkeypatch.setattr(database, 'DEFAULT_DB_PATH', funded_db)
    bot = bot_for(member(), member('restricted', 0))
    asyncio.run(_process_sleep_action(message(bot), 1, 2))
    assert get_balance(-100, 1) == 100
    assert get_sleep_action('sleep:-100:88').status == 'refunded'
    bot.restrict_chat_member.assert_not_awaited()

def test_recovery_preserves_stronger_admin_policy(funded_db, monkeypatch):
    monkeypatch.setattr(database, 'DEFAULT_DB_PATH', funded_db)
    charge = charge_sleep(-100, 1, 2, operation_key='pending', until_date=int(time.time())+180,
                          duration_minutes=3, extended=False)
    bot = bot_for(member('restricted', 0))
    asyncio.run(_apply_sleep(bot, -100, 1, 2, charge))
    bot.restrict_chat_member.assert_not_awaited()
    assert get_sleep_action('pending').status == 'applied'

def test_antispam_preserves_policy(monkeypatch):
    monkeypatch.setattr(middlewares, 'save_user_data', lambda *a: None)
    bot = bot_for(member('restricted', int(time.time())+86400))
    event = message(bot)
    handler = AsyncMock()
    asyncio.run(AntiSpamMiddleware(limit=0)(handler, event, {}))
    bot.restrict_chat_member.assert_not_awaited()
    event.answer.assert_not_awaited()
    handler.assert_not_awaited()
