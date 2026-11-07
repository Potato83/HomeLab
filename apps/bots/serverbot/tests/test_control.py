import asyncio
import fcntl
import importlib.util
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from serverbot.config import ConfigError, load_config
from serverbot.control import ControlClient, ControlError, Target
from serverbot.control_handlers import allowed_actions, card, panel_callback
from serverbot.middlewares import AccessMiddleware


@pytest.fixture
def host(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('host_control_test', Path(__file__).resolve().parents[1] / 'deploy/host/control.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'BASE', tmp_path)
    boot = tmp_path / 'boot-id'
    boot.write_text('this-boot')
    monkeypatch.setattr(module, 'BOOT_ID', boot)
    return module


@pytest.mark.parametrize('args', [[], ['sshd', 'stop'], ['serverbot', 'stop'],
                                 ['kali', 'destroy'], ['chatbot;id', 'start'],
                                 ['chatbot', 'errors', '/etc/shadow'], ['kali', 'errors']])
def test_helper_rejects_unapproved_commands(host, monkeypatch, args):
    calls = MagicMock(side_effect=AssertionError('must not execute'))
    monkeypatch.setattr(host, 'run', calls)
    with pytest.raises(host.ControlError, match='invalid_action'):
        host.dispatch(args)
    calls.assert_not_called()


def test_manual_stop_only_suppresses_its_dependencies_after_verified_stop(host, monkeypatch):
    host.save_intent('kali', 'stopped')
    monkeypatch.setattr(host, 'observed_state', lambda target: 'stopped')
    assert host.monitoring_overrides() == {'vm:kali': host.STOPPED_DETAIL, 'vm:ssh': host.STOPPED_DETAIL}
    monkeypatch.setattr(host, 'observed_state', lambda target: 'running')
    assert host.monitoring_overrides() == {}
    assert host.read_intent('kali') == {}
    host.save_intent('kali', 'stopped')
    monkeypatch.setattr(host, 'observed_state', lambda target: 'unknown')
    assert host.monitoring_overrides() == {}


def test_old_boot_and_expired_operation_do_not_hide_failures(host, monkeypatch):
    host.save_intent('chatbot', 'stopped')
    host.BOOT_ID.write_text('next-boot')
    assert host.read_intent('chatbot') == {}
    host.save_intent('workbot', 'busy')
    intent = host.read_intent('workbot')
    intent['deadline'] = time.time() - 1
    (host.BASE / 'workbot.json').write_text(json.dumps(intent))
    assert host.monitoring_overrides() == {}


def test_stop_is_fixed_command_verified_and_persisted(host, monkeypatch):
    states = iter(['running', 'stopped', 'stopped'])
    monkeypatch.setattr(host, 'observed_state', lambda target: next(states))
    run = MagicMock(return_value=(0, ''))
    monkeypatch.setattr(host, 'run', run)
    assert host.execute('workbot', 'stop')['manual'] is True
    run.assert_called_once_with(['/sbin/rc-service', 'workbot', 'stop'], timeout=110)
    assert host.read_intent('workbot')['state'] == 'stopped'


def test_kali_restart_verifies_shutdown_before_start(host, monkeypatch):
    monkeypatch.setattr(host, 'observed_state', lambda target: 'running')
    events = []
    monkeypatch.setattr(host, 'run', lambda args, timeout: (events.append(args[-2]) or 0, ''))
    monkeypatch.setattr(host, 'wait_for', lambda target, expected, timeout: events.append(expected))
    assert host.execute('kali', 'restart')['state'] == 'running'
    assert events == ['shutdown', 'stopped', 'start', 'running']
    assert host.read_intent('kali') == {}


def test_failed_stop_does_not_report_success_or_leave_busy_intent(host, monkeypatch):
    monkeypatch.setattr(host, 'observed_state', lambda target: 'running')
    monkeypatch.setattr(host, 'run', lambda *a, **kw: (1, 'secret must never escape'))
    with pytest.raises(host.ControlError, match='command_failed'):
        host.execute('chatbot', 'stop')
    assert host.read_intent('chatbot') == {}


def test_unconfirmed_shutdown_times_out_without_starting_vm(host, monkeypatch):
    monkeypatch.setattr(host, 'observed_state', lambda target: 'running')
    run = MagicMock(return_value=(0, ''))
    monkeypatch.setattr(host, 'run', run)
    monkeypatch.setattr(host, 'wait_for', MagicMock(side_effect=host.ControlError('state_timeout')))
    with pytest.raises(host.ControlError, match='state_timeout'):
        host.execute('kali', 'restart')
    assert run.call_count == 1
    assert run.call_args.args[0][-2:] == ['shutdown', 'kali-ctf']
    assert host.read_intent('kali') == {}


def test_state_change_or_second_process_cannot_execute_an_operation(host, monkeypatch):
    run = MagicMock()
    monkeypatch.setattr(host, 'run', run)
    monkeypatch.setattr(host, 'observed_state', lambda target: 'stopped')
    with pytest.raises(host.ControlError, match='state_changed'):
        host.execute('chatbot', 'restart')
    with (host.BASE / 'chatbot.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(host.ControlError, match='busy'):
            host.execute('chatbot', 'start')
    run.assert_not_called()


def test_error_view_omits_info_keeps_traceback_and_redacts_credentials(host):
    text = host.filter_errors('''2026-11-07 19:00:00 INFO app polling started
2026-11-07 19:00:01 WARNING app request slow
2026-11-07 19:00:02 ERROR app handler failed token=secret-value
Traceback (most recent call last):
  File "handler.py", line 4, in handle
    raise ValueError("bad")
ValueError: bad
2026-11-07 19:00:03 INFO app request complete
''')
    assert 'polling started' not in text and 'request complete' not in text
    assert 'WARNING' in text and 'Traceback' in text and 'File "handler.py"' in text and 'ValueError: bad' in text
    assert 'secret-value' not in text and '[REDACTED]' in text
    assert host.filter_errors('INFO normal startup\nINFO finished') == ''
    assert host.filter_errors('INFO request handled with error response') == ''
    assert 'warning payload' in host.filter_errors('{"level":40,"msg":"warning payload"}')
    assert host.filter_errors('{"level":"info","msg":"ERROR is a user input"}') == ''


def test_log_tail_is_bounded_and_cannot_follow_symlinks(host, tmp_path):
    path = tmp_path / 'log'
    path.write_bytes(b'x' * 200000 + b'\nERROR last failure\n')
    text = host.read_tail(path)
    assert len(text) < 131073 and 'last failure' in text
    link = tmp_path / 'link'
    link.symlink_to(path)
    with pytest.raises(OSError):
        host.read_tail(link)


def test_unknown_state_never_looks_stopped_or_offers_actions():
    row = Target('kali', 'unknown')
    text, markup = card(row)
    assert 'неизвестно' in text
    assert allowed_actions(row) == ()
    assert not any('ask:' in button.callback_data for line in markup.inline_keyboard for button in line)


def test_confirmations_bind_user_chat_message_and_are_single_use(monkeypatch):
    client = ControlClient(True)
    token = client.confirm(123, 123, 1, 'kali', 'stop')
    for owner in ((456, 123, 1), (123, 456, 1), (123, 123, 2)):
        with pytest.raises(ControlError):
            client.consume(token, *owner)
    assert client.consume(token, 123, 123, 1).action == 'stop'
    with pytest.raises(ControlError):
        client.consume(token, 123, 123, 1)
    token = client.confirm(123, 123, 1, 'kali', 'stop')
    now = time.monotonic()
    monkeypatch.setattr('serverbot.control.time.monotonic', lambda: now + 61)
    with pytest.raises(ControlError):
        client.consume(token, 123, 123, 1)


def test_cancel_or_replaced_confirmation_invalidates_old_buttons():
    client = ControlClient(True)
    old = client.confirm(123, 123, 1, 'kali', 'stop')
    new = client.confirm(123, 123, 1, 'kali', 'restart')
    with pytest.raises(ControlError):
        client.consume(old, 123, 123, 1)
    client.forget_message(123, 1)
    with pytest.raises(ControlError):
        client.consume(new, 123, 123, 1)


def test_client_rejects_false_success_and_parallel_mutations(monkeypatch):
    async def run():
        client = ControlClient(True)
        client.request = AsyncMock(return_value=dict(target='chatbot', state='running', manual=False))
        with pytest.raises(ControlError, match='подтвердить'):
            await client.execute('chatbot', 'stop')
        assert client.busy == set()
        client.busy.add('chatbot')
        with pytest.raises(ControlError, match='уже выполняется'):
            await client.execute('chatbot', 'stop')
        assert client.request.await_count == 1
    asyncio.run(run())


def callback(data):
    message = MagicMock(spec=Message)
    message.chat = SimpleNamespace(id=123)
    message.message_id = 1
    message.edit_text = AsyncMock()
    message.answer = AsyncMock()
    return SimpleNamespace(data=data, message=message, from_user=SimpleNamespace(id=123), answer=AsyncMock())


def test_confirmation_does_not_execute_until_confirmed_and_replay_is_ignored():
    async def run():
        client = ControlClient(True)
        client.status = AsyncMock(return_value=Target('kali', 'running'))
        client.execute = AsyncMock(return_value=Target('kali', 'stopped', True))
        ask = callback('control:ask:kali:stop')
        await panel_callback(ask, client)
        client.execute.assert_not_awaited()
        assert 'Подтвердить' in str(ask.message.edit_text.await_args)
        token = next(iter(client.pending))
        confirmed = callback('control:do:' + token)
        await panel_callback(confirmed, client)
        client.execute.assert_awaited_once_with('kali', 'stop')
        await panel_callback(confirmed, client)
        client.execute.assert_awaited_once()
    asyncio.run(run())


@pytest.mark.parametrize(('user_id', 'chat_type', 'expected_calls'), [(123, 'private', 1), (456, 'private', 0), (123, 'group', 0)])
def test_real_dispatcher_routes_panel_callbacks_and_applies_access_checks(user_id, chat_type, expected_calls):
    async def run():
        from aiogram import Router
        from serverbot.handlers import is_status_callback, status_callback_handler
        from serverbot.control_handlers import is_control_callback
        root, panel = Router(), Router()
        root.callback_query.register(status_callback_handler, is_status_callback)
        panel.callback_query.register(panel_callback, is_control_callback)
        dp = Dispatcher()
        dp.update.outer_middleware(AccessMiddleware(frozenset({123})))
        dp.include_routers(root, panel)
        client = ControlClient(True)
        client.targets = AsyncMock(return_value=[Target('chatbot', 'running')])
        bot = Bot('123456:' + 'A' * 35)
        bot.session = AsyncMock()
        user = User(id=user_id, is_bot=False, first_name='Test')
        message = Message(message_id=1, date=datetime.now(UTC), chat=Chat(id=user_id, type=chat_type), text='panel')
        event = CallbackQuery(id='cb', from_user=user, chat_instance='test', message=message, data='control:home')
        await asyncio.wait_for(dp.feed_update(bot, Update(update_id=1, callback_query=event), control=client), timeout=3)
        assert client.targets.await_count == expected_calls
    asyncio.run(run())


@pytest.mark.parametrize('value', ['"yes"', '1', '[]'])
def test_control_configuration_requires_boolean(tmp_path, value):
    path = tmp_path / 'config.toml'
    path.write_text('[[checks.disk]]\npath="/"\n[control]\nenabled=' + value)
    with pytest.raises(ConfigError, match='control.enabled'):
        load_config(path)


def test_helper_transport_has_fixed_executable_and_hides_raw_errors(monkeypatch):
    async def run():
        process = SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(
            b'{"ok":false,"error":"command_failed"}', b'private-secret')))
        create = AsyncMock(return_value=process)
        monkeypatch.setattr('serverbot.control.asyncio.create_subprocess_exec', create)
        client = ControlClient(True)
        with pytest.raises(ControlError, match='Команда завершилась'):
            await client.execute('chatbot', 'stop')
        assert create.await_args.args == (
            '/usr/bin/doas', '-n', '/usr/local/lib/homelab/control.py', 'chatbot', 'stop')
        client.enabled = False
        with pytest.raises(ControlError, match='не включено'):
            await client.request('list')
        assert create.await_count == 1
    asyncio.run(run())
