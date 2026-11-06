from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from workbot.config import load_config
from workbot.settings import Settings, effective_config, handle_settings
from workbot.runner import Monitor
from workbot.models import Vacancy
from workbot.matching import decide
from workbot.storage import Store
from workbot.telegram import Telegram
from workbot.sites import length

ROOT = Path(__file__).resolve().parents[1]

class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / 'db')
        self.store = Store(self.path)
        self.base = load_config(str(ROOT/'config.toml'))
        self.manager = Settings(self.base, self.store)
        self.seq = 0
        self.v = Vacancy('mts','1','Стажёр SOC','МТС','https://example.com',cities=['Санкт-Петербург'],work_format='hybrid',level='intern')

    def tearDown(self):
        self.store.close();self.temp.cleanup()

    def command(self,args,command='/set'):
        self.seq += 1
        return self.manager.execute(command,args,self.seq)

    def config(self): return effective_config(self.base,self.store)

    def test_multiple_schedule_windows_are_saved_and_invalid_change_is_rejected(self):
        value = 'mon,fri 09:00-12:00; sun 22:00-03:00'
        self.assertIn('сохранены', self.command('schedule ' + value))
        self.assertEqual(self.config()['monitor']['schedule'], value)
        self.command('schedule mon 09:00-09:00')
        self.assertEqual(self.config()['monitor']['schedule'], value)

    def test_city_formats_levels_keywords_and_exclusions(self):
        self.assertIn('сохранены',self.command('city Москва'))
        self.assertFalse(decide(self.v,self.config()['filters']).matched)
        self.command('city СПб | Москва')
        self.assertTrue(decide(self.v,self.config()['filters']).matched)
        self.command('formats remote')
        self.assertFalse(decide(self.v,self.config()['filters']).matched)
        self.v.work_format='remote'
        self.command('levels junior')
        self.assertFalse(decide(self.v,self.config()['filters']).matched)
        self.command('levels intern,junior')
        self.command('include pentest')
        self.assertFalse(decide(self.v,self.config()['filters']).matched)
        self.command('include SOC | pentest')
        self.assertTrue(decide(self.v,self.config()['filters']).matched)
        self.command('exclude soc')
        self.assertFalse(decide(self.v,self.config()['filters']).matched)
        self.command('exclude -')
        self.assertTrue(decide(self.v,self.config()['filters']).matched)

    def test_unknown_location_and_spb_alias(self):
        self.command('city СПб')
        self.assertTrue(decide(self.v,self.config()['filters']).matched)
        self.v.cities=[]
        self.command('unknown off')
        self.assertFalse(decide(self.v,self.config()['filters']).matched)
        self.command('unknown on')
        self.assertTrue(decide(self.v,self.config()['filters']).matched)

    def test_invalid_values_do_not_change_settings(self):
        before=self.config()
        for args in ['interval 0','interval NaN','pause maybe','formats field','levels senior','city '+ 'a'*81,'include '+'x'*61,'unknown yes','URL https://example.com']:
            self.command(args)
            self.assertEqual(self.config(),before)

    def test_replay_and_restart_preserve_latest_setting(self):
        original=self.manager.execute('/set','city Москва',10)
        self.manager.execute('/set','city Казань',11)
        self.assertEqual(self.manager.execute('/set','city Москва',10),original)
        self.store.close();self.store=Store(self.path)
        self.manager=Settings(self.base,self.store)
        self.assertEqual(self.config()['filters']['city_label'],'Казань')
        self.assertNotIn('city_label',self.base['filters'])

    def test_reset_restores_toml_and_does_not_clear_delivery_history(self):
        self.store.save_snapshot('mts',[(self.v,decide(self.v,self.base['filters']))])
        row=self.store.pending(['mts'],20)[0];self.store.mark_sent(row)
        self.command('city Москва');self.command('', '/settings_reset')
        self.assertEqual(self.config(),self.base)
        self.assertFalse(self.store.pending(['mts'],20))

    def test_source_switches_intervals_unverified_and_token_guard(self):
        self.command('mts off','/source');self.assertFalse(self.config()['sources']['mts']['enabled'])
        self.command('mts interval 120','/source');self.assertEqual(self.config()['sources']['mts']['interval_seconds'],7200)
        self.command('pt_fast_track unverified off','/source');self.assertFalse(self.config()['sources']['pt_fast_track']['notify_unverified'])
        with patch.dict('os.environ',{'HH_API_TOKEN':''}):
            self.assertIn('HH_API_TOKEN',self.command('hh on','/source'))
            self.assertFalse(self.config()['sources']['hh']['enabled'])
        for name in self.base['sources']:
            self.command(name+' off','/source')
        self.assertFalse(any(s['enabled'] for s in self.config()['sources'].values()))

    def test_pending_queue_refiltered_without_resending_confirmed(self):
        self.store.save_snapshot('mts',[(self.v,decide(self.v,self.base['filters']))])
        monitor=Monitor(self.base,self.store,Mock(),delivery_delay=0)
        self.command('city Москва');monitor.reload_settings()
        self.assertFalse(self.store.pending(['mts'],20))
        self.command('city СПб');monitor.reload_settings()
        row=self.store.pending(['mts'],20)[0];self.store.mark_sent(row)
        self.command('city Москва');monitor.reload_settings()
        self.command('city СПб');monitor.reload_settings()
        self.assertFalse(self.store.pending(['mts'],20))

    def test_pause_blocks_automatic_scans_and_delivery(self):
        self.command('pause on')
        monitor=Monitor(self.base,self.store,Mock())
        with patch('workbot.runner.scan') as scan:
            monitor.run_due();scan.assert_not_called()
        self.assertEqual(monitor._dispatch(),{'alerts':0,'vacancies':0})

    def test_per_source_intervals(self):
        base=deepcopy(self.base)
        for name,s in base['sources'].items():s['enabled']=name in ('mts','croc')
        base['sources']['mts']['interval_seconds']=60
        base['sources']['croc']['interval_seconds']=120
        clock=[100]
        monitor=Monitor(base,self.store,Mock(),clock=lambda:clock[0],delivery_delay=0)
        captured=[]
        def fake(config,store):
            captured.append({name for name,s in config['sources'].items() if s['enabled']})
            return {'sources':{}}
        with patch('workbot.runner.scan',side_effect=fake):
            monitor.run_due();clock[0]=160;monitor.run_due()
        self.assertEqual(captured,[{'mts','croc'},{'mts'}])
        self.assertEqual(monitor.next_scan,220)

    def test_owner_only_and_settings_output_bounded(self):
        telegram=Telegram('fake','123');telegram.send=Mock()
        monitor=Monitor(self.base,self.store,telegram)
        update={'update_id':1,'message':{'from': {'id': 123}, 'chat':{'id':456,'type':'private'},'text':'/set city Москва'}}
        self.assertFalse(handle_settings(telegram,monitor,update))
        self.assertEqual(monitor.config,self.base)
        self.command('include '+' | '.join('&'*60+str(i) for i in range(20))) # rejected length
        self.command('include '+' | '.join('&'*57+str(i) for i in range(32)))
        self.command('exclude '+' | '.join('<'*57+str(i) for i in range(32)))
        monitor.reload_settings()
        update['message']={'from': {'id': 123}, 'chat':{'id':123,'type':'private'},'text':'/settings all'}
        self.assertTrue(handle_settings(telegram,monitor,update))
        self.assertTrue(all(length(call.args[0])<=3500 for call in telegram.send.call_args_list))
        self.assertGreater(telegram.send.call_count,1)

    def test_reply_failure_replay_does_not_undo_later_change(self):
        telegram=Telegram('fake','123');telegram.send=Mock()
        monitor=Monitor(self.base,self.store,telegram)
        update={'update_id':1,'message':{'from': {'id': 123}, 'chat':{'id':123,'type':'private'},'text':'/set city Москва'}}
        handle_settings(telegram,monitor,update)
        next_update={'update_id':2,'message':{'from': {'id': 123}, 'chat':{'id':123,'type':'private'},'text':'/set city Казань'}}
        handle_settings(telegram,monitor,next_update);handle_settings(telegram,monitor,update)
        self.assertEqual(monitor.config['filters']['city_label'],'Казань')

    def test_digest_mode_and_invalid_command_do_not_reset_schedule(self):
        monitor=Monitor(self.base,self.store,Mock(),clock=lambda:100)
        self.command('delivery digest');self.command('digest 30');monitor.reload_settings()
        self.assertEqual(monitor.config['monitor']['digest_seconds'],1800)
        monitor.next_scan=500
        self.command('formats nonsense');monitor.reload_settings()
        self.assertEqual(monitor.next_scan,500)

    def test_saved_settings_cli_is_read_only_and_needs_no_telegram(self):
        from contextlib import redirect_stdout
        from io import StringIO
        from workbot.__main__ import main
        import json
        self.command('city Москва');before=self.store.db.total_changes
        output=StringIO()
        with patch.dict('os.environ',{'WORKBOT_DB':self.path}),patch('sys.argv',['workbot','settings','--json']),patch('workbot.__main__.Telegram') as tg,patch('workbot.__main__.logging.basicConfig'),redirect_stdout(output):
            self.assertEqual(main(),0);tg.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())['filters']['city_label'],'Москва')
        self.assertEqual(before,self.store.db.total_changes)

if __name__=='__main__':unittest.main()
