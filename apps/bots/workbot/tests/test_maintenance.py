from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import selectors
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from workbot.__main__ import main
from workbot.config import load_config
from workbot.locking import InstanceRunning, instance_lock
from workbot.maintenance import BackupError, backup_database
from workbot.models import Vacancy, Decision
from workbot.storage import Store

ROOT = Path(__file__).resolve().parents[1]


class Fixture:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)
        self.path = self.directory / 'bot.sqlite3'
        self.store = Store(str(self.path))
        v = Vacancy('mts', '1', 'Junior SOC', 'МТС', 'https://example.org/1')
        self.store.save_snapshot('mts', [(v, Decision(True, 'ИБ'))])
        self.store.mark_sent(self.store.pending(['mts'], 20)[0])
        self.store.set_state('telegram_offset', 42)
        self.store.source_error('croc', 'Тестовый HTTP 503')
        self.target = self.directory / 'backups' / 'copy.sqlite3'
        self.env = {'WORKBOT_DB': str(self.path), 'WORKBOT_CONFIG': str(ROOT / 'config.toml'),
                    'BOT_TOKEN': 'test-token', 'OWNER_ID': '42'}

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()


class BackupTests(Fixture, unittest.TestCase):
    def test_live_wal_backup_preserves_history_and_health(self):
        self.assertTrue(Path(str(self.path) + '-wal').exists())
        before = list(self.store.db.iterdump())
        with instance_lock(self.path):
            result = backup_database(self.path, self.target)
        self.assertEqual(list(self.store.db.iterdump()), before)
        self.assertEqual(result['sha256'], hashlib.sha256(self.target.read_bytes()).hexdigest())
        self.assertEqual(result['bytes'], self.target.stat().st_size)
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o600)
        db = sqlite3.connect(str(self.target))
        try:
            self.assertEqual(db.execute('PRAGMA journal_mode').fetchone()[0], 'delete')
            self.assertEqual(db.execute('PRAGMA quick_check').fetchone()[0], 'ok')
            self.assertEqual(db.execute("SELECT value FROM state WHERE key='telegram_offset'").fetchone()[0], '42')
            self.assertEqual(db.execute("SELECT failure_count FROM source_health WHERE source='croc'").fetchone()[0], 1)
        finally:
            db.close()
        restored = Store(str(self.target))
        try:
            self.assertFalse(restored.pending(['mts'], 20))
        finally:
            restored.close()

    def test_uncommitted_writer_data_is_not_copied(self):
        self.store.db.execute("UPDATE state SET value='99' WHERE key='telegram_offset'")
        self.assertTrue(self.store.db.in_transaction)
        try:
            backup_database(self.path, self.target)
            db = sqlite3.connect(str(self.target))
            try:
                self.assertEqual(db.execute("SELECT value FROM state WHERE key='telegram_offset'").fetchone()[0], '42')
            finally:
                db.close()
        finally:
            self.store.db.rollback()

    def test_existing_destination_is_never_overwritten(self):
        self.target.parent.mkdir()
        self.target.write_bytes(b'keep this file')
        with self.assertRaises(FileExistsError):
            backup_database(self.path, self.target)
        self.assertEqual(self.target.read_bytes(), b'keep this file')

    def test_source_and_its_symlink_cannot_be_destination(self):
        alias = self.directory / 'alias.sqlite3'
        alias.symlink_to(self.path)
        for target in [self.path, alias]:
            with self.subTest(target=target), self.assertRaises(BackupError):
                backup_database(self.path, target)
        self.assertEqual(self.store.get_state('telegram_offset'), '42')

    def test_dangling_destination_symlink_not_replaced(self):
        self.target.parent.mkdir()
        self.target.symlink_to(self.directory / 'missing')
        with self.assertRaises(FileExistsError):
            backup_database(self.path, self.target)
        self.assertTrue(self.target.is_symlink())

    def test_database_sidecar_names_cannot_be_destinations(self):
        for suffix in ['-wal', '-shm', '-journal', '.lock', '.instance.lock']:
            with self.subTest(suffix=suffix), self.assertRaises(BackupError):
                backup_database(self.path, str(self.path) + suffix)

    def test_missing_or_foreign_source_does_not_publish_backup(self):
        foreign = self.directory / 'foreign.sqlite3'
        db = sqlite3.connect(str(foreign))
        db.execute('CREATE TABLE another_app(value INTEGER)')
        db.commit()
        db.close()
        for source in [self.directory / 'missing.sqlite3', foreign]:
            with self.subTest(source=source), self.assertRaises(BackupError):
                backup_database(source, self.target)
        self.assertFalse(self.target.exists())
        self.assertFalse(self.target.parent.exists())

    def test_destination_race_does_not_overwrite_other_file(self):
        real_link = os.link
        def raced_link(source, destination):
            Path(destination).write_bytes(b'created concurrently')
            return real_link(source, destination)
        with patch('workbot.maintenance.os.link', side_effect=raced_link):
            with self.assertRaises(FileExistsError):
                backup_database(self.path, self.target)
        self.assertEqual(self.target.read_bytes(), b'created concurrently')
        self.assertEqual(list(self.target.parent.glob('.workbot-backup-*')), [])

    def test_timeout_cleans_temporary_files(self):
        with patch('workbot.maintenance.time.monotonic', side_effect=[0, 31, 32]):
            with self.assertRaises(BackupError):
                backup_database(self.path, self.target)
        self.assertFalse(self.target.exists())
        self.assertEqual(list(self.target.parent.iterdir()), [])
        self.assertEqual(self.store.get_state('telegram_offset'), '42')

    def test_publish_error_leaves_source_and_destination_intact(self):
        with patch('workbot.maintenance.os.link', side_effect=OSError('filesystem refused publication')):
            with self.assertRaises(OSError):
                backup_database(self.path, self.target)
        self.assertFalse(self.target.exists())
        self.assertEqual(list(self.target.parent.iterdir()), [])

    def test_cli_backup_needs_neither_telegram_nor_source_configuration(self):
        output = io.StringIO()
        with (patch('sys.argv', ['workbot', 'backup', '--output', str(self.target), '--json']),
              patch.dict(os.environ, {'WORKBOT_DB': str(self.path)}, clear=True),
              patch('workbot.__main__.load_env'), patch('workbot.__main__.logging.basicConfig'),
              patch('workbot.__main__.load_config') as config,
              patch('workbot.__main__.Store') as store,
              patch('workbot.__main__.Telegram') as telegram, redirect_stdout(output)):
            self.assertEqual(main(), 0)
        config.assert_not_called()
        store.assert_not_called()
        telegram.assert_not_called()
        self.assertTrue(json.loads(output.getvalue())['ok'])


class LockTests(Fixture, unittest.TestCase):
    def test_lock_is_exclusive_and_persistent_file_does_not_block_restart(self):
        with instance_lock(self.path):
            with self.assertRaises(InstanceRunning):
                with instance_lock(self.path):
                    pass
        self.assertTrue(Path(str(self.path) + '.instance.lock').exists())
        with instance_lock(self.path):
            pass

    def test_symlink_and_relative_alias_share_lock(self):
        alias = self.directory / 'alias.sqlite3'
        alias.symlink_to(self.path)
        relative = os.path.relpath(self.path, Path.cwd())
        with instance_lock(self.path):
            for value in [alias, relative]:
                with self.subTest(value=value), self.assertRaises(InstanceRunning):
                    with instance_lock(value):
                        pass

    def test_lock_released_after_exception(self):
        with self.assertRaises(ValueError):
            with instance_lock(self.path):
                raise ValueError('test')
        with instance_lock(self.path):
            pass

    def test_independent_process_and_abrupt_exit(self):
        script = ('import sys\nfrom workbot.locking import instance_lock\n'
                  'with instance_lock(sys.argv[1]):\n print("locked", flush=True)\n sys.stdin.readline()\n')
        child = subprocess.Popen([sys.executable, '-u', '-c', script, str(self.path)], cwd=ROOT,
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(child.stdout, selectors.EVENT_READ)
                self.assertTrue(selector.select(timeout=5), 'Child did not acquire the lock')
            self.assertEqual(child.stdout.readline().strip(), 'locked')
            with self.assertRaises(InstanceRunning):
                with instance_lock(self.path):
                    pass
            child.kill()
            child.communicate(timeout=5)
            with instance_lock(self.path):
                pass
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)

    def test_run_and_once_are_rejected_before_store_or_network(self):
        for command in ['run', 'once']:
            with self.subTest(command=command), instance_lock(self.path):
                with (patch('sys.argv', ['workbot', command]), patch.dict(os.environ, self.env, clear=True),
                      patch('workbot.__main__.load_env'), patch('workbot.__main__.logging.basicConfig'),
                      patch('workbot.__main__.Store') as store, patch('workbot.__main__.Monitor') as monitor):
                    with self.assertRaises(InstanceRunning):
                        main()
                store.assert_not_called()
                monitor.assert_not_called()

    def test_chat_id_cannot_compete_with_running_bot(self):
        with instance_lock(self.path):
            with (patch('sys.argv', ['workbot', 'chat-id']), patch.dict(os.environ, self.env, clear=True),
                  patch('workbot.__main__.load_env'), patch('workbot.__main__.logging.basicConfig'),
                  patch('workbot.__main__.telegram_call') as rpc):
                with self.assertRaises(InstanceRunning):
                    main()
            rpc.assert_not_called()

    def test_status_still_available_while_instance_is_running(self):
        with instance_lock(self.path):
            with (patch('sys.argv', ['workbot', 'status', '--json']), patch.dict(os.environ, self.env, clear=True),
                  patch('workbot.__main__.load_env'), patch('workbot.__main__.logging.basicConfig'),
                  redirect_stdout(io.StringIO())):
                self.assertEqual(main(), 0)

    def test_run_keeps_instance_lock_during_long_polling(self):
        handlers = {}
        telegram = Mock()
        def register(signum, handler):
            handlers[signum] = handler
        def updates(offset):
            with self.assertRaises(InstanceRunning):
                with instance_lock(self.path):
                    pass
            handlers[signal.SIGTERM](signal.SIGTERM, None)
            return []
        telegram.updates.side_effect = updates
        with (patch('sys.argv', ['workbot', 'run']), patch.dict(os.environ, self.env, clear=True),
              patch('workbot.__main__.load_env'), patch('workbot.__main__.logging.basicConfig'),
              patch('workbot.__main__.Telegram', return_value=telegram),
              patch('workbot.__main__.Monitor'), patch('workbot.__main__.signal.signal', side_effect=register)):
            self.assertEqual(main(), 0)
        with instance_lock(self.path):
            pass

    def test_store_failure_releases_instance_lock(self):
        with (patch('sys.argv', ['workbot', 'run']), patch.dict(os.environ, self.env, clear=True),
              patch('workbot.__main__.load_env'), patch('workbot.__main__.logging.basicConfig'),
              patch('workbot.__main__.Store', side_effect=sqlite3.DatabaseError('broken'))):
            with self.assertRaises(sqlite3.DatabaseError):
                main()
        with instance_lock(self.path):
            pass


if __name__ == '__main__':
    unittest.main()
