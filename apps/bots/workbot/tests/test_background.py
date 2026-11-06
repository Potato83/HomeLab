from copy import deepcopy
from pathlib import Path
import tempfile
from threading import Event, get_ident
import unittest
from unittest.mock import Mock,patch
from workbot.background import BackgroundScan
from workbot.config import load_config
from workbot.matching import decide
from workbot.models import Vacancy
from workbot.runner import Monitor
from workbot.settings import Settings
from workbot.storage import Store
from workbot.telegram import Telegram

ROOT=Path(__file__).resolve().parents[1]

class BackgroundTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(str(Path(self.tmp.name)/'db'));self.addCleanup(self.store.close)
        self.config=load_config(str(ROOT/'config.toml'))
        for key,source in self.config['sources'].items():source['enabled']=key=='mts'
        self.telegram=Mock();self.seconds=100
        self.monitor=Monitor(self.config,self.store,self.telegram,clock=lambda:self.seconds,delivery_delay=0)
        self.addCleanup(self.monitor.close)
        self.started,self.release=Event(),Event()
        self.v=Vacancy('mts','1','Junior DevOps','MTS','https://example.com',work_format='remote',level='junior')
        self.worker_id=None

    def collect(self,config):
        self.worker_id=get_ident();self.started.set();self.release.wait(2)
        v=deepcopy(self.v)
        return {'sources':{'mts':{'error':None,'found':1,'matched':1}},'vacancies':[{'vacancy':v.to_dict()}]}

    def factory(self,config,revision,due):return BackgroundScan(config,revision,due,collect=self.collect)

    def test_schedule_blocks_automatic_scan_but_allows_manual_check(self):
        with patch('workbot.runner.allows_scan', return_value=False), patch('workbot.background.BackgroundScan', side_effect=self.factory) as factory:
            self.assertFalse(self.monitor.request_check(force=False))
            self.assertEqual(self.monitor.next_scan, self.seconds + 60)
            factory.assert_not_called()
            self.assertTrue(self.monitor.request_check(force=True))
            self.release.set()
            self.monitor.background.thread.join(2)

    def complete(self):
        self.release.set();self.monitor.background.thread.join(2)
        self.monitor.run_background()

    def test_network_thread_has_no_database_and_only_one_scan_is_started(self):
        with patch('workbot.background.BackgroundScan',side_effect=self.factory) as factory:
            self.assertTrue(self.monitor.request_check());self.assertTrue(self.started.wait(1))
            self.assertNotEqual(self.worker_id,get_ident())
            self.assertFalse(self.monitor.request_check());self.assertEqual(factory.call_count,1)
            self.assertEqual(self.store.db.execute('SELECT count(*) FROM vacancies').fetchone()[0],0)
            self.complete()
            self.assertEqual(self.telegram.send.call_count,1)
            self.assertEqual(self.store.pending(['mts'],20),[])
            self.monitor.run_background();self.assertEqual(factory.call_count,1)

    def test_settings_and_owner_commands_work_while_network_is_blocked(self):
        with patch('workbot.background.BackgroundScan',side_effect=self.factory):
            self.monitor.request_check();self.started.wait(1)
            telegram=Telegram('fake','123');telegram.send=Mock()
            from workbot.settings import handle_settings
            update={'update_id':1,'message':{'from': {'id': 123}, 'chat':{'id':123,'type':'private'},'text':'/set directions security'}}
            self.assertTrue(handle_settings(telegram,self.monitor,update))
            self.assertEqual(self.monitor.config['filters']['directions'],['security'])
            self.assertTrue(self.monitor.background.cancelled.is_set())
            self.complete()
            # The result computed before the change must not replace a source snapshot.
            self.assertEqual(self.store.db.execute('SELECT count(*) FROM vacancies').fetchone()[0],0)
            self.telegram.send.assert_not_called()

    def test_pause_discards_inflight_result_and_does_not_start_another_scan(self):
        with patch('workbot.background.BackgroundScan',side_effect=self.factory) as factory:
            self.monitor.request_check();self.started.wait(1)
            Settings(self.config,self.store).execute('/set','pause on',1);self.monitor.reload_settings()
            self.complete();self.monitor.run_background()
            self.assertIsNone(self.monitor.background);self.assertEqual(factory.call_count,1)
            self.telegram.send.assert_not_called()
            self.assertEqual(self.store.db.execute('SELECT count(*) FROM source_status').fetchone()[0],0)

    def test_worker_errors_are_sanitized_and_retried_on_source_schedule(self):
        def failed(config):raise RuntimeError('https://api.telegram.org/botSECRET/getMe')
        with patch('workbot.background.BackgroundScan',side_effect=lambda c,r,d:BackgroundScan(c,r,d,collect=failed)):
            self.monitor.request_check();self.monitor.background.thread.join(1);self.monitor.run_background()
            self.assertIsNone(self.monitor.background)
            self.assertNotIn('SECRET',self.store.get_state('last_scan_error'))
            self.assertEqual(self.monitor.next_scan,3700)

    def test_snapshot_error_preserves_previous_rows_and_holds_delivery(self):
        self.store.save_snapshot('mts',[(self.v,decide(self.v,self.config['filters']))])
        result={'sources':{'mts':{'error':'Неполная выдача'}},'vacancies':[]}
        with patch('workbot.background.BackgroundScan',side_effect=lambda c,r,d:BackgroundScan(c,r,d,collect=lambda cfg:result)):
            self.monitor.request_check();self.monitor.background.thread.join(1);self.monitor.run_background()
            self.assertEqual(len(self.store.pending(['mts'],20)),1)
            self.telegram.send.assert_not_called()
            self.assertEqual(self.store.status()[0]['error'],'Неполная выдача')

    def test_close_cancels_network_without_waiting_for_it(self):
        with patch('workbot.background.BackgroundScan',side_effect=self.factory):
            self.monitor.request_check();self.started.wait(1)
            job=self.monitor.background;self.monitor.close()
            self.assertTrue(job.cancelled.is_set());self.release.set();job.thread.join(1)
            self.assertEqual(self.store.db.execute('SELECT count(*) FROM vacancies').fetchone()[0],0)

    def test_pending_rows_wait_for_their_current_source_scan(self):
        self.store.save_snapshot('mts',[(self.v,decide(self.v,self.config['filters']))])
        with patch('workbot.background.BackgroundScan',side_effect=self.factory):
            self.monitor.request_check();self.started.wait(1)
            self.assertEqual(self.monitor.dispatch_pending()['vacancies'],0)
            self.assertEqual(len(self.store.pending(['mts'],20)),1)
            self.complete()
            self.assertEqual(self.telegram.send.call_count,1)

    def test_delivery_retry_does_not_restart_finished_scan(self):
        from workbot.http import FetchError
        self.telegram.send.side_effect=FetchError('offline')
        with patch('workbot.background.BackgroundScan',side_effect=self.factory) as factory:
            self.monitor.request_check();self.started.wait(1)
            with self.assertRaises(FetchError):self.complete()
            self.assertEqual(self.monitor.next_scan,3700)
            self.seconds=161
            with self.assertRaises(FetchError):self.monitor.run_background()
            self.assertEqual(factory.call_count,1)

if __name__=='__main__':unittest.main()
