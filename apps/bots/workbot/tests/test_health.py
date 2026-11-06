from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import signal
import re
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from botwork.config import load_config
from botwork.health import deliver_health, notifications
from botwork.http import FetchError
from botwork.matching import decide
from botwork.models import Vacancy
from botwork.reporting import source_views, sources_text, status_text
from botwork.runner import Monitor
from botwork.storage import Store

ROOT = Path(__file__).resolve().parents[1]
START = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)


class HealthFixture:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'bot.sqlite3')
        self.store = Store(self.path)
        self.config = load_config(str(ROOT / 'config.toml'))
        self.config['alerts'] = {'enabled': True, 'failure_threshold': 3, 'min_failure_seconds': 60}
        self.telegram = Mock()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def fail(self, seconds=0, source='mts', error='HTTP 503'):
        with patch('botwork.storage.now', return_value=(START + timedelta(seconds=seconds)).isoformat()):
            self.store.source_error(source, error)

    def success(self, seconds=0, source='mts', include_vacancy=False):
        rows = []
        if include_vacancy:
            v = Vacancy(source, '1', 'Junior SOC', 'Компания', 'https://example.org/1', work_format='remote')
            rows = [(v, decide(v, self.config['filters']))]
        with patch('botwork.storage.now', return_value=(START + timedelta(seconds=seconds)).isoformat()):
            self.store.save_snapshot(source, rows)

    def three_failures(self):
        self.fail(0)
        self.fail(30)
        self.fail(60)

    def send(self, seconds=60):
        return deliver_health(self.store, self.telegram, self.config,
                              at=START + timedelta(seconds=seconds), delay=0)


class HealthTests(HealthFixture, unittest.TestCase):
    def test_both_count_and_duration_thresholds_required(self):
        self.fail()
        self.fail(1)
        self.assertEqual(self.send(3600), 0)
        self.fail(2)
        self.assertEqual(self.send(59), 0)
        self.assertEqual(self.send(60), 1)

    def test_continuous_outage_alerted_once_even_if_error_changes(self):
        self.three_failures()
        self.assertEqual(self.send(), 1)
        self.fail(120, error='Изменилась разметка')
        self.assertEqual(self.send(300), 0)
        self.assertEqual(self.telegram.send.call_count, 1)

    def test_restart_preserves_confirmed_alert(self):
        self.three_failures()
        self.send()
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.send(300), 0)
        self.assertEqual(self.store.status()[0]['failure_count'], 3)

    def test_failed_alert_remains_pending_after_restart(self):
        self.three_failures()
        self.telegram.send.side_effect = FetchError('Telegram недоступен')
        with self.assertRaises(FetchError):
            self.send()
        self.store.close()
        self.store = Store(self.path)
        self.telegram.send.side_effect = None
        self.assertEqual(self.send(300), 1)

    def test_recovery_sent_once_after_confirmed_alert(self):
        self.three_failures()
        self.send()
        self.success(90)
        self.assertEqual(self.send(90), 1)
        self.assertIn('восстановилась', self.telegram.send.call_args.args[0])
        self.success(120)
        self.assertEqual(self.send(120), 0)

    def test_recovery_without_alert_is_silent(self):
        self.fail()
        self.success(90)
        self.assertEqual(self.send(90), 0)

    def test_failed_alert_then_recovery_does_not_send_obsolete_warning(self):
        self.three_failures()
        self.telegram.send.side_effect = FetchError('offline')
        with self.assertRaises(FetchError):
            self.send()
        self.telegram.send.side_effect = None
        self.success(90)
        self.assertEqual(self.send(90), 0)

    def test_failed_recovery_retried(self):
        self.three_failures()
        self.send()
        self.success(90)
        self.telegram.send.side_effect = FetchError('offline')
        with self.assertRaises(FetchError):
            self.send(90)
        self.telegram.send.side_effect = None
        self.assertEqual(self.send(120), 1)

    def test_new_outage_can_alert_after_recovery(self):
        self.three_failures()
        self.send()
        self.success(90)
        self.send(90)
        for seconds in [120, 150, 180]:
            self.fail(seconds)
        self.assertEqual(self.send(180), 1)
        self.assertEqual(self.store.status()[0]['incident'], 2)

    def test_recovery_still_reported_after_short_second_outage(self):
        self.three_failures()
        self.send()
        self.success(90)
        self.fail(100)
        self.success(110)
        self.assertEqual(self.send(110), 1)
        self.assertIn('восстановилась', self.telegram.send.call_args.args[0])

    def test_disabled_source_or_alerts_are_silent(self):
        self.three_failures()
        self.config['sources']['mts']['enabled'] = False
        self.assertEqual(self.send(), 0)
        self.config['sources']['mts']['enabled'] = True
        self.config['alerts']['enabled'] = False
        self.assertEqual(self.send(), 0)

    def test_html_in_errors_is_escaped(self):
        self.three_failures()
        self.fail(90, error='<script>bad</script>')
        self.send(90)
        text = self.telegram.send.call_args.args[0]
        self.assertNotIn('<script>', text)
        self.assertIn('&lt;script&gt;', text)

    def test_status_shows_new_disabled_failed_and_held_queue(self):
        self.success(include_vacancy=True)
        self.fail(60)
        self.config['sources']['croc']['enabled'] = False
        views = {row['source']: row for row in source_views(self.store, self.config)}
        self.assertEqual(views['mts']['state'], 'error')
        self.assertEqual(views['mts']['pending'], 1)
        self.assertEqual(views['croc']['state'], 'disabled')
        self.assertEqual(views['yandex']['state'], 'unchecked')
        text = status_text(self.store, self.config)
        self.assertIn('из-за ошибок: 1', text)
        self.assertIn('HTTP 503', text)
        self.assertIn('job.mts.ru', sources_text(self.store, self.config))

    def test_legacy_error_backfilled_conservatively(self):
        path = str(Path(self.tmp.name) / 'legacy.sqlite3')
        db = sqlite3.connect(path)
        db.execute('CREATE TABLE source_status(source TEXT PRIMARY KEY, last_attempt TEXT, last_success TEXT, error TEXT, found INTEGER, matched INTEGER)')
        db.execute('INSERT INTO source_status VALUES (?, ?, ?, ?, ?, ?)',
                   ('mts', START.isoformat(), None, 'HTTP 503', None, None))
        db.commit()
        db.close()
        old = Store(path)
        try:
            self.assertEqual(old.status()[0]['failure_count'], 1)
            self.assertEqual(notifications(old, self.config, START + timedelta(days=1)), [])
        finally:
            old.close()

    def test_old_configuration_gets_alert_defaults(self):
        text = (ROOT / 'config.toml').read_text()
        text = re.sub(r'\n\[alerts\][\s\S]*?(?=\n\[filters\])', '', text)
        path = Path(self.tmp.name) / 'old.toml'
        path.write_text(text)
        config = load_config(str(path))
        self.assertEqual(config['alerts'], {'enabled': True, 'failure_threshold': 3, 'min_failure_seconds': 3600})

    def test_invalid_alert_settings_rejected(self):
        text = (ROOT / 'config.toml').read_text()
        path = Path(self.tmp.name) / 'bad.toml'
        for original, replacement in [('failure_threshold = 3', 'failure_threshold = 0'),
                                      ('failure_threshold = 3', 'failure_threshold = true'),
                                      ('min_failure_seconds = 3600', 'min_failure_seconds = -1')]:
            with self.subTest(replacement=replacement):
                path.write_text(text.replace(original, replacement))
                with self.assertRaises(ValueError):
                    load_config(str(path))


class RunnerTests(HealthFixture, unittest.TestCase):
    def make_monitor(self):
        self.seconds = 100.0
        self.config['alerts']['enabled'] = False
        return Monitor(self.config, self.store, self.telegram, clock=lambda: self.seconds, delivery_delay=0)

    def fake_scan(self, config, store):
        self.success(include_vacancy=True)
        return {'sources': {'mts': {'error': None, 'found': 1, 'matched': 1}}}

    def test_delivery_failure_does_not_rescan_sites(self):
        monitor = self.make_monitor()
        self.telegram.send.side_effect = FetchError('offline')
        with patch('botwork.runner.scan', side_effect=self.fake_scan) as scan:
            with self.assertRaises(FetchError):
                monitor.run_due()
            self.assertEqual(monitor.next_scan, 3700)
            self.seconds = 161
            with self.assertRaises(FetchError):
                monitor.run_due()
            self.assertEqual(scan.call_count, 1)
            self.assertEqual(monitor.next_delivery, 221)
            self.seconds = 200
            monitor.run_due()
            self.assertEqual(scan.call_count, 1)
            self.assertIn('offline', status_text(self.store, self.config))

    def test_source_checks_remain_due_despite_polling_errors(self):
        from botwork.__main__ import main
        self.make_monitor()
        handlers = {}
        calls = 0
        def register(signum, handler):
            handlers[signum] = handler
        def updates(offset):
            nonlocal calls
            calls += 1
            if calls == 1:
                return []
            if calls == 2:
                self.seconds = 3700
                raise FetchError('getUpdates offline')
            handlers[signal.SIGTERM](signal.SIGTERM, None)
            return []
        self.telegram.updates.side_effect = updates
        def factory(config, store, telegram):
            return Monitor(config, store, telegram, clock=lambda: self.seconds, delivery_delay=0)
        def finished_job(config, revision, due):
            job = Mock(revision=revision, due=due)
            job.poll.return_value = ({'sources': {'mts': {'error': None}}, 'vacancies': []}, None)
            return job
        with (patch('sys.argv', ['botwork', 'run']),
              patch.dict(os.environ, {'BOTWORK_DB': self.path}),
              patch('botwork.__main__.load_env'),
              patch('botwork.__main__.load_config', return_value=self.config),
              patch('botwork.__main__.Store', return_value=self.store),
              patch('botwork.__main__.Telegram', return_value=self.telegram),
              patch('botwork.__main__.Monitor', side_effect=factory),
              patch('botwork.__main__.logging.basicConfig'),
              patch('botwork.__main__.signal.signal', side_effect=register),
              patch('botwork.__main__.time.sleep'),
              patch('botwork.background.BackgroundScan', side_effect=finished_job) as jobs):
            self.assertEqual(main(), 0)
            self.assertEqual(jobs.call_count, 2)

    def test_healthy_source_queue_is_sent_failed_source_is_held(self):
        monitor = self.make_monitor()
        self.success(source='mts', include_vacancy=True)
        self.success(source='croc', include_vacancy=True)
        self.fail(60, source='mts')
        sent = monitor.dispatch_pending()
        self.assertEqual(sent['vacancies'], 1)
        self.assertEqual(len(self.store.pending(['mts'], 20)), 1)
        self.assertEqual(self.store.pending(['croc'], 20), [])

    def test_alert_failure_does_not_block_vacancy_attempt(self):
        monitor = self.make_monitor()
        self.success(include_vacancy=True)
        with patch('botwork.runner.deliver_health', side_effect=FetchError('alert rejected')):
            with self.assertRaises(FetchError):
                monitor.dispatch_pending()
        self.assertEqual(self.store.pending(['mts'], 20), [])


if __name__ == '__main__':
    unittest.main()
