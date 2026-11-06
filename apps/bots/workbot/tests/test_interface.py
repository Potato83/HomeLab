from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from botwork.config import load_config
from botwork.interface import handle_controls, panel_token, settings_panel
from botwork.runner import Monitor
from botwork.telegram import Telegram


class InterfaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        from botwork.storage import Store
        self.store = Store(str(Path(self.tmp.name) / 'db'))
        self.addCleanup(self.store.close)
        self.telegram = Telegram('dummy', '123')
        self.telegram.send = Mock()
        self.telegram.answer_callback = Mock()
        config = load_config(str(Path(__file__).resolve().parents[1] / 'config.toml'))
        self.monitor = Monitor(config, self.store, self.telegram)
        self.addCleanup(self.monitor.close)

    def callback(self, data, seq=1, sender=123):
        return {'update_id': seq, 'callback_query': {'id': 'dummy', 'data': data + ':' + panel_token(self.monitor),
                'from': {'id': sender}, 'message': {'from': {'id': 123}, 'chat': {'id': 123, 'type': 'private'}}}}

    def test_controls_authorization_and_replay(self):
        self.assertFalse(handle_controls(self.telegram, self.monitor, self.callback('ui:pause:on', sender=999)))
        self.assertFalse(self.monitor.config['monitor'].get('paused', False))
        handle_controls(self.telegram, self.monitor, self.callback('ui:pause:on'))
        self.assertTrue(self.monitor.config['monitor']['paused'])
        handle_controls(self.telegram, self.monitor, self.callback('ui:pause:off', seq=2))
        handle_controls(self.telegram, self.monitor, self.callback('ui:pause:on'))
        self.assertFalse(self.monitor.config['monitor']['paused'])

    def test_navigation_requires_owner_and_panel_is_compact(self):
        update = {'message': {'from': {'id': 123}, 'chat': {'id': 999, 'type': 'private'}, 'text': '⚙️ Настройки'}}
        self.assertIsNone(self.telegram.command(update))
        update['message']['chat']['id'] = 123
        self.assertEqual(self.telegram.command(update), '/settings')
        text, keyboard = settings_panel(self.monitor)
        self.assertIn('/settings all', text)
        self.assertLess(len(text), 3500)
        self.assertEqual(len(keyboard['inline_keyboard']), 9)

    def test_old_panel_cannot_change_new_profile_settings(self):
        old = self.callback('ui:levels:intern')
        self.store.set_state('active_search_profile', 'Другой профиль')
        self.assertTrue(handle_controls(self.telegram, self.monitor, old))
        self.telegram.answer_callback.assert_called_once()
        self.assertIn('изменились', self.telegram.answer_callback.call_args.args[1])
        self.assertIsNone(self.store.get_state('search_settings'))
