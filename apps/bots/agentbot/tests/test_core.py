"""Dependency-free recovery checks; also collected by pytest."""
import asyncio
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from agentbot.access import owner_allowed
from agentbot.service import Service, completion
from agentbot.store import Store


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'state.db'
        self.store = Store(self.path)
        self.store.add_session('ses_test')
        self.store.prepare(1, 'ses_test', 'private fixture')
        self.backend = AsyncMock()
        self.backend.statuses.return_value = {}
        self.send = AsyncMock()
        self.service = Service(self.store, self.backend, self.send)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def test_durable_preparation_and_restart(self):
        self.store.update(1, state='submitting')
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.current(), 'ses_test')
        self.assertEqual(self.store.get(1)['state'], 'unknown')
        self.assertEqual(self.store.get(1)['prompt'], 'private fixture')
        self.assertFalse(self.store.prepare(1, 'ses_test', 'duplicate'))
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    async def test_timeout_then_reconcile(self):
        self.backend.submit.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            await self.service.tick()
        self.assertEqual(self.store.get(1)['state'], 'unknown')
        mid = self.store.get(1)['message']
        self.backend.messages.return_value = [
            {'info': {'id': mid, 'role': 'user'}},
            {'info': {'role': 'assistant', 'parentID': mid, 'finish': 'stop', 'time': {'completed': 1}},
             'parts': [{'type': 'text', 'text': '4'}]}]
        await self.service.tick()
        self.backend.submit.assert_awaited_once()
        self.assertEqual(self.store.get(1)['state'], 'done')
        self.assertIsNone(self.store.get(1)['prompt'])
        self.assertEqual(self.store.get(1)['notified'], 1)
        self.assertTrue(self.send.call_args.args[0].endswith('4'))

    async def test_unknown_never_replays(self):
        self.store.update(1, state='unknown')
        self.backend.messages.return_value = []
        await self.service.tick()
        self.backend.submit.assert_not_awaited()
        self.assertEqual(self.store.get(1)['prompt'], 'private fixture')

    async def test_delivery_cursor_survives_failure(self):
        self.store.update(1, state='done', answer='a' * 7001)
        self.backend.statuses.side_effect = TimeoutError()
        self.send.side_effect = [None, TimeoutError()]
        with self.assertRaises(TimeoutError):
            await self.service.tick()
        self.assertEqual(self.store.get(1)['sent'], 3500)
        self.send.reset_mock(side_effect=True)
        await self.service.tick()
        self.assertEqual(self.send.await_count, 2)
        self.assertEqual(self.store.get(1)['sent'], 7001)
        self.assertEqual(self.store.get(1)['notified'], 1)

    async def test_stop_during_outage(self):
        self.store.update(1, state='running', stop_requested=1)
        self.backend.abort.side_effect = [TimeoutError(), None]
        with self.assertRaises(TimeoutError):
            await self.service.tick()
        self.assertEqual(self.store.get(1)['stop_requested'], 1)
        await self.service.tick()
        self.assertEqual(self.store.get(1)['state'], 'stopped')

    async def test_stop_before_submit(self):
        self.store.update(1, stop_requested=1)
        self.backend.statuses.side_effect = TimeoutError()
        await self.service.tick()
        self.backend.submit.assert_not_awaited()
        self.backend.abort.assert_not_awaited()
        self.assertEqual(self.store.get(1)['state'], 'stopped')

    async def test_completion_matches_turn_and_waits_for_tools(self):
        messages = [{'info': {'role': 'assistant', 'parentID': 'old', 'time': {'completed': 1}}}]
        self.assertIsNone(completion(messages, 'new', {}))
        messages[0]['info'].update(parentID='new', finish='tool-calls')
        self.assertIsNone(completion(messages, 'new', {}))
        messages[0]['info']['finish'] = 'stop'
        self.assertIsNone(completion(messages, 'new', {'type': 'busy'}))
        self.assertEqual(completion(messages, 'new', {})[0], 'done')

    async def test_watch_shutdown_does_not_abort_backend(self):
        self.store.update(1, state='running')
        self.backend.statuses.side_effect = TimeoutError()
        task = asyncio.create_task(self.service.watch())
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.backend.abort.assert_not_awaited()
        self.assertEqual(self.store.get(1)['state'], 'running')

    async def test_owner_access_boundary(self):
        self.assertTrue(owner_allowed(123, 123, 123, 'private'))
        for user, chat, kind in [(456, 456, 'private'), (123, -1, 'group'),
                                 (None, 123, 'private'), (123, 456, 'private'),
                                 (123, None, None)]:
            self.assertFalse(owner_allowed(123, user, chat, kind))
        self.assertFalse(owner_allowed(0, 0, 0, 'private'))

    async def test_notification_belongs_to_its_task_when_another_is_selected(self):
        self.store.add_session('ses_other', title='Other')
        self.store.update(1, state='done', answer='result')
        specific = AsyncMock()
        await Service(self.store, self.backend, self.send, send_task=specific).tick()
        self.send.assert_not_awaited()
        self.assertEqual(self.store.current(), 'ses_other')
        self.assertEqual(specific.call_args.args[0], 'ses_test')
        self.assertTrue(specific.call_args.args[1].startswith('#1 ·'))
        self.assertNotIn('ses_test', specific.call_args.args[1])
