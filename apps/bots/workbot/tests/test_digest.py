from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch
from workbot.config import load_config
from workbot.models import Vacancy
from workbot.matching import decide
from workbot.runner import Monitor
from workbot.storage import Store
from workbot.telegram import deliver_digest
from workbot.http import FetchError
from workbot.sites import length

ROOT=Path(__file__).resolve().parents[1]

class DigestTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=str(Path(self.temp.name)/'db');self.store=Store(self.path)
        self.config=load_config(str(ROOT/'config.toml'));self.config['monitor'].update(delivery_mode='digest',digest_seconds=60)
        self.telegram=Mock();self.v=Vacancy('mts','1','Стажёр SOC','МТС','https://example.com/1',cities=['Санкт-Петербург'],work_format='hybrid',level='intern')
        self.store.save_snapshot('mts',[(self.v,decide(self.v,self.config['filters']))])
    def tearDown(self):self.store.close();self.temp.cleanup()
    def test_period_survives_restart_and_sends_once(self):
        monitor=Monitor(self.config,self.store,self.telegram,delivery_delay=0)
        with patch('workbot.runner.time.time',return_value=100):
            monitor._dispatch();self.telegram.send.assert_not_called()
        self.store.close();self.store=Store(self.path);monitor=Monitor(self.config,self.store,self.telegram,delivery_delay=0)
        with patch('workbot.runner.time.time',return_value=159):monitor._dispatch();self.telegram.send.assert_not_called()
        with patch('workbot.runner.time.time',return_value=160):self.assertEqual(monitor._dispatch()['vacancies'],1)
        with patch('workbot.runner.time.time',return_value=161):self.assertEqual(monitor._dispatch()['vacancies'],0)
        self.assertEqual(self.telegram.send.call_count,1)
    def test_failure_retried_without_advancing_schedule(self):
        monitor=Monitor(self.config,self.store,self.telegram,delivery_delay=0)
        self.store.set_state('digest_next_at',100);self.telegram.send.side_effect=[FetchError('offline'),None]
        with patch('workbot.runner.time.time',return_value=101):
            with self.assertRaises(FetchError):monitor._dispatch()
        self.assertEqual(self.store.get_state('digest_next_at'),'100')
        with patch('workbot.runner.time.time',return_value=102):self.assertEqual(monitor._dispatch()['vacancies'],1)
        self.assertFalse(self.store.pending(['mts'],20))
    def test_large_digest_marks_only_accepted_chunks(self):
        records=[]
        for n in range(20):
            v=Vacancy('mts',str(n),'Стажёр SOC '+str(n),'МТС','https://example.com/'+str(n),cities=['СПб'],work_format='hybrid',notes=['X'*350],level='intern')
            records.append((v,decide(v,self.config['filters'])))
        self.store.save_snapshot('mts',records)
        self.telegram.send.side_effect=[None,FetchError('offline')]
        with self.assertRaises(FetchError):deliver_digest(self.store,self.telegram,['mts'],20,delay=0)
        self.assertTrue(all(length(call.args[0])<=3500 for call in self.telegram.send.call_args_list))
        pending=len(self.store.pending(['mts'],20));self.assertTrue(0<pending<20)
        self.telegram.send.side_effect=None
        self.assertEqual(deliver_digest(self.store,self.telegram,['mts'],20,delay=0),pending)
    def test_unverified_warning_and_application_link_kept(self):
        self.v.recruitment_status='unknown';self.v.application_url='https://example.com/form'
        self.store.save_snapshot('mts',[(self.v,decide(self.v,self.config['filters'],True))])
        deliver_digest(self.store,self.telegram,['mts'],20,delay=0)
        text=self.telegram.send.call_args.args[0]
        self.assertIn('Набор не подтверждён',text);self.assertIn('https://example.com/form',text)

if __name__=='__main__':unittest.main()
