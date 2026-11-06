from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from workbot.__main__ import main
from workbot.config import load_config
from workbot.diagnostics import database_check, diagnose, format_report, send_test_message
from workbot.http import FetchError, telegram_call
from workbot.models import Vacancy, Decision
from workbot.storage import Store
from workbot.telegram import Telegram

ROOT = Path(__file__).resolve().parents[1]
TOKEN = '123456:EXAMPLE_SECRET_TOKEN_FOR_TESTS'


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)
        self.db_path = self.directory / 'nested' / 'bot.sqlite3'
        self.env = {'BOT_TOKEN': TOKEN, 'OWNER_ID': '42',
                    'WORKBOT_DB': str(self.db_path), 'WORKBOT_CONFIG': str(ROOT / 'config.toml')}

    def tearDown(self):
        self.tmp.cleanup()

    def rpc(self, token, method, payload):
        return {'getMe': {'id': 123456, 'is_bot': True},
                'getWebhookInfo': {'url': ''},
                'getChat': {'id': 42, 'type': 'private'}}[method]

    def states(self, report):
        return {item['check']: item['state'] for item in report['checks']}

    def test_offline_uses_no_network_and_does_not_create_history(self):
        with patch('workbot.diagnostics.telegram_call') as rpc, patch('workbot.diagnostics.scan') as scan:
            report = diagnose(env=self.env)
        self.assertTrue(report['ok'])
        self.assertEqual(report['mode'], 'offline')
        rpc.assert_not_called()
        scan.assert_not_called()
        self.assertFalse(self.db_path.exists())
        self.assertEqual(list(self.db_path.parent.iterdir()), [])
        self.assertNotIn(TOKEN, json.dumps(report))

    def test_missing_credentials_are_actionable(self):
        report = diagnose(env=self.env | {'BOT_TOKEN': '', 'OWNER_ID': ''})
        self.assertFalse(report['ok'])
        self.assertEqual(self.states(report)['telegram_token'], 'error')
        self.assertIn('chat-id', format_report(report))

    def test_invalid_chat_id_rejected_without_requests(self):
        for value in ['-10042', '@channel', '0', '42 1']:
            with self.subTest(value=value), patch('workbot.diagnostics.telegram_call') as rpc:
                report = diagnose(env=self.env | {'OWNER_ID': value})
                self.assertEqual(self.states(report)['telegram_chat'], 'error')
                rpc.assert_not_called()
                with self.assertRaises(ValueError):
                    Telegram(TOKEN, value)

    def test_broken_configuration_reported_without_traceback(self):
        path = self.directory / 'bad.toml'
        path.write_text('[monitor\n')
        report = diagnose(str(path), env=self.env)
        self.assertEqual(self.states(report)['config'], 'error')

    def test_environment_error_is_reported_and_redacted(self):
        report = diagnose(env=self.env, env_error='Cannot load ' + TOKEN)
        self.assertFalse(report['ok'])
        self.assertNotIn(TOKEN, format_report(report))

    def test_directory_and_corrupt_database_rejected(self):
        with self.assertRaises(ValueError):
            database_check(str(self.directory))
        self.db_path.parent.mkdir()
        self.db_path.write_text('not sqlite')
        report = diagnose(env=self.env)
        self.assertEqual(self.states(report)['database'], 'error')

    def test_foreign_sqlite_file_is_not_modified(self):
        self.db_path.parent.mkdir()
        db = sqlite3.connect(str(self.db_path))
        db.execute('CREATE TABLE another_app(value TEXT)')
        db.commit()
        db.close()
        before = self.db_path.read_bytes()
        report = diagnose(env=self.env)
        self.assertEqual(self.states(report)['database'], 'error')
        self.assertEqual(self.db_path.read_bytes(), before)

    def test_existing_bot_database_and_delivery_history_unchanged(self):
        store = Store(str(self.db_path))
        v = Vacancy('mts', '1', 'Junior SOC', 'МТС', 'https://example.org/1')
        store.save_snapshot('mts', [(v, Decision(True, 'ИБ'))])
        row = store.pending(['mts'], 20)[0]
        store.mark_sent(row)
        store.set_state('telegram_offset', 17)
        store.close()
        before = self.db_path.read_bytes()
        report = diagnose(env=self.env)
        self.assertTrue(report['ok'])
        self.assertEqual(before, self.db_path.read_bytes())
        store = Store(str(self.db_path))
        try:
            self.assertFalse(store.pending(['mts'], 20))
            self.assertEqual(store.get_state('telegram_offset'), '17')
        finally:
            store.close()

    def test_online_telegram_checks_only_read_methods(self):
        with patch('workbot.diagnostics.telegram_call', side_effect=self.rpc) as rpc:
            report = diagnose(env=self.env, online=True, skip_sources=True)
        self.assertTrue(report['ok'])
        self.assertEqual([call.args[1] for call in rpc.call_args_list], ['getMe', 'getWebhookInfo', 'getChat'])
        self.assertEqual(self.states(report)['sources_network'], 'skipped')

    def test_webhook_conflict_is_reported_without_disclosing_url(self):
        def rpc(token, method, payload):
            return {'url': 'https://private.example/secret-hook'} if method == 'getWebhookInfo' else self.rpc(token, method, payload)
        with patch('workbot.diagnostics.telegram_call', side_effect=rpc) as call:
            report = diagnose(env=self.env, online=True, skip_sources=True)
        self.assertFalse(report['ok'])
        self.assertEqual(self.states(report)['telegram_polling'], 'error')
        self.assertNotIn('secret-hook', json.dumps(report))
        self.assertNotIn('deleteWebhook', [item.args[1] for item in call.call_args_list])

    def test_invalid_token_skips_chat_access(self):
        with patch('workbot.diagnostics.telegram_call', side_effect=FetchError('Telegram HTTP 401')) as rpc:
            report = diagnose(env=self.env, online=True, skip_sources=True)
        self.assertFalse(report['ok'])
        self.assertEqual(rpc.call_count, 1)
        self.assertEqual(self.states(report)['telegram_access'], 'skipped')

    def test_group_or_mismatched_chat_response_rejected(self):
        for response in [{'id': 42, 'type': 'group'}, {'id': 99, 'type': 'private'}]:
            with self.subTest(response=response):
                def rpc(token, method, payload):
                    return response if method == 'getChat' else self.rpc(token, method, payload)
                with patch('workbot.diagnostics.telegram_call', side_effect=rpc):
                    report = diagnose(env=self.env, online=True, skip_sources=True)
                self.assertEqual(self.states(report)['telegram_access'], 'error')

    def test_online_source_checks_do_not_use_store(self):
        result = {'sources': {'mts': {'error': None, 'found': 39, 'matched': 1},
                              'croc': {'error': 'HTTP 503'}}}
        with patch('workbot.diagnostics.telegram_call', side_effect=self.rpc), patch('workbot.diagnostics.scan', return_value=result) as scan:
            report = diagnose(env=self.env, online=True)
        self.assertFalse(report['ok'])
        self.assertEqual(self.states(report)['source:mts'], 'ok')
        self.assertEqual(self.states(report)['source:croc'], 'error')
        self.assertEqual(len(scan.call_args.args), 1)
        self.assertEqual(scan.call_args.kwargs, {})
        self.assertFalse(self.db_path.exists())

    def test_errors_cannot_echo_token(self):
        with patch('workbot.diagnostics.telegram_call', side_effect=FetchError('request /bot' + TOKEN)):
            report = diagnose(env=self.env, online=True, skip_sources=True)
        self.assertNotIn(TOKEN, json.dumps(report))
        self.assertNotIn(TOKEN.partition(':')[2], format_report(report))

    def test_explicit_test_message_sends_exactly_once(self):
        with patch('workbot.diagnostics.telegram_call', side_effect=self.rpc), patch('workbot.telegram.telegram_call', return_value={'message_id': 1}) as send:
            result = send_test_message(TOKEN, '42')
        self.assertTrue(result['ok'])
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.args[1], 'sendMessage')
        self.assertEqual(send.call_args.args[2]['chat_id'], '42')
        self.assertIn('тестовое уведомление', send.call_args.args[2]['text'])

    def test_test_message_not_sent_if_chat_validation_fails(self):
        def rpc(token, method, payload):
            return {'id': 42, 'type': 'group'} if method == 'getChat' else self.rpc(token, method, payload)
        with patch('workbot.diagnostics.telegram_call', side_effect=rpc), patch('workbot.telegram.telegram_call') as send:
            with self.assertRaises(FetchError):
                send_test_message(TOKEN, '42')
        send.assert_not_called()

    def test_doctor_cli_reports_config_error_as_json(self):
        missing = str(self.directory / 'missing.toml')
        output = io.StringIO()
        with (patch('sys.argv', ['workbot', 'doctor', '--config', missing, '--json']),
              patch.dict(os.environ, self.env, clear=True), patch('workbot.__main__.load_env'),
              patch('workbot.__main__.logging.basicConfig'),
              redirect_stdout(output)):
            self.assertEqual(main(), 1)
        report = json.loads(output.getvalue())
        self.assertEqual(self.states(report)['config'], 'error')

    def test_test_telegram_cli_does_not_require_config_or_open_database(self):
        output = io.StringIO()
        with (patch('sys.argv', ['workbot', 'test-telegram', '--json']),
              patch.dict(os.environ, self.env, clear=True), patch('workbot.__main__.load_env'),
              patch('workbot.__main__.logging.basicConfig'),
              patch('workbot.__main__.load_config') as config,
              patch('workbot.__main__.Store') as store,
              patch('workbot.diagnostics.telegram_call', side_effect=self.rpc),
              patch('workbot.telegram.telegram_call', return_value={}), redirect_stdout(output)):
            self.assertEqual(main(), 0)
        config.assert_not_called()
        store.assert_not_called()
        self.assertTrue(json.loads(output.getvalue())['ok'])

    def test_source_settings_and_invalid_timeouts_rejected_locally(self):
        text = (ROOT / 'config.toml').read_text()
        path = self.directory / 'bad.toml'
        cases = [('request_timeout_seconds = 25', 'request_timeout_seconds = nan'),
                 ('max_messages_per_cycle = 20', 'max_messages_per_cycle = 1.5'),
                 ('page_size = 100', 'page_size = 0'),
                 ('https://job.mts.ru/api/v2/vacancies', 'http://job.mts.ru/api/v2/vacancies'),
                 ('https://job.mts.ru/api/v2/vacancies', 'https://job.mts.ru:wrong/api/v2/vacancies'),
                 ('https://careers.croc.ru/vacancies/', 'https://user:password@careers.croc.ru/vacancies/')]
        for original, replacement in cases:
            with self.subTest(replacement=replacement):
                path.write_text(text.replace(original, replacement))
                with self.assertRaises(ValueError):
                    load_config(str(path))


class TelegramResponseTests(unittest.TestCase):
    def test_malformed_api_response_is_sanitized(self):
        for value in [[], {}, {'ok': True}, {'ok': False, 'error_code': TOKEN}]:
            with self.subTest(value=value), patch('workbot.http.urlopen'), patch('workbot.http.json.load', return_value=value):
                with self.assertRaises(FetchError) as error:
                    telegram_call(TOKEN, 'getMe', {})
            self.assertNotIn(TOKEN, str(error.exception))

    def test_invalid_request_cannot_expose_token(self):
        with patch('workbot.http.Request', side_effect=ValueError('bad URL ' + TOKEN)):
            with self.assertRaises(FetchError) as error:
                telegram_call(TOKEN, 'getMe', {})
        self.assertNotIn(TOKEN, str(error.exception))


if __name__ == '__main__':
    unittest.main()
