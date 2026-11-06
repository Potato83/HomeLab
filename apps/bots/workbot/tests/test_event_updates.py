from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import Mock, patch

from botwork.config import load_config, validate_config
from botwork.event_details import enrich_terms
from botwork.events import decide_event, collect_ctftime, collect_ods, parse_ctftime
from botwork.http import FetchError
from botwork.models import Vacancy
from botwork.runner import Monitor
from botwork.settings import Settings, effective_config
from botwork.storage import Store
from botwork.telegram import format_message, deliver_digest
from tests.test_events import NOW, event, ctf, ods

ROOT=Path(__file__).resolve().parents[1]

class EventUpdateTests(unittest.TestCase):
    def setUp(self):
        self.config=load_config(str(ROOT/'config.toml'));self.rules=self.config['filters']
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=str(Path(self.temp.name)/'db');self.store=Store(self.path);self.addCleanup(lambda:self.store.close())
        self.clock=patch('botwork.events.event_now',return_value=NOW);self.clock.start();self.addCleanup(self.clock.stop)

    def save(self,v):self.store.save_snapshot(v.source,[(v,decide_event(v,self.rules))],rules=self.rules)
    def pending(self,source='ctftime'):return self.store.pending([source],20)
    def shown(self,v=None):
        v=v or event();self.save(v);self.store.mark_sent(self.pending(v.source)[0]);return v

    def test_explicit_labels_only_no_language_from_english_title(self):
        v=enrich_terms(event(description='Free T-shirts. We discuss English-language books.'))
        self.assertEqual(v.language_codes,[]);self.assertEqual(v.price_kind,'unknown')
        v=enrich_terms(event(description='Язык: Русский / English\nСтоимость участия: бесплатно'))
        self.assertEqual(v.language_codes,['en','ru']);self.assertEqual(v.price_kind,'free')
        v=enrich_terms(event(description='Language:\nEnglish\nEntry fee: 1000 RUB'))
        self.assertEqual(v.language_codes,['en']);self.assertEqual(v.price_kind,'paid')

    def test_conditional_free_and_conflicting_prices_are_unknown(self):
        for description in ['Стоимость: бесплатно для студентов','Стоимость: бесплатно\nСтоимость: 1000 ₽','Стоимость: по запросу','Entry fee: free for qualifiers']:
            self.assertEqual(enrich_terms(event(description=description)).price_kind,'unknown',description)
        self.assertEqual(enrich_terms(event(description='Стоимость: 0 ₽')).price_kind,'free')
        self.assertEqual(enrich_terms(event(description='Участие бесплатно.')).price_kind,'free')
        self.assertEqual(enrich_terms(event(description='Ticket price: $10')).price_kind,'paid')
        self.assertEqual(enrich_terms(event(description='Ticket price: $0')).price_kind,'free')
        self.assertEqual(enrich_terms(event(description='Стоимость: 0 ₽ для студентов, 1000 ₽ для остальных')).price_kind,'unknown')

    def test_price_and_language_filters_have_explicit_unknown_choices(self):
        v=event()
        self.assertFalse(decide_event(v,self.rules|{'event_price':'free'}).matched)
        d=decide_event(v,self.rules|{'event_price':'free_or_unknown'});self.assertTrue(d.matched);self.assertTrue(any('стоимость' in x for x in d.warnings))
        self.assertFalse(decide_event(v,self.rules|{'event_languages':['ru']}).matched)
        self.assertTrue(decide_event(v,self.rules|{'event_languages':['ru','unknown']}).matched)
        v=enrich_terms(event(description='Language: English\nTicket price: 500 RUB'))
        self.assertTrue(decide_event(v,self.rules|{'event_price':'paid','event_languages':['en']}).matched)
        self.assertFalse(decide_event(v,self.rules|{'event_price':'free'}).matched)

    def test_settings_persist_refilter_and_replay(self):
        manager=Settings(self.config,self.store)
        for n,args in enumerate(['event_languages ru,unknown','event_price free_or_unknown','event_cancellations off'],1):
            self.assertIn('сохранены',manager.execute('/set',args,n))
        f=effective_config(self.config,self.store)['filters'];self.assertEqual(f['event_languages'],['ru','unknown']);self.assertEqual(f['event_price'],'free_or_unknown');self.assertFalse(f['event_cancellations'])
        manager.execute('/set','event_price free',10);manager.execute('/set','event_price any',11);manager.execute('/set','event_price free',10)
        self.assertEqual(effective_config(self.config,self.store)['filters']['event_price'],'any')
        self.store.close();self.store=Store(self.path);self.assertEqual(effective_config(self.config,self.store)['filters']['event_languages'],['ru','unknown'])

    def test_invalid_settings_are_atomic(self):
        manager=Settings(self.config,self.store);before=effective_config(self.config,self.store)
        for n,args in enumerate(['event_price cheap','event_languages any,ru','event_languages javascript','event_cancellations maybe'],1):
            manager.execute('/set',args,n);self.assertEqual(effective_config(self.config,self.store),before)
        for key,value in [('event_price',{}),('event_languages',['any','ru']),('event_languages',[{}]),('event_cancellations',1)]:
            c=deepcopy(self.config);c['filters'][key]=value
            with self.assertRaises(ValueError):validate_config(c)

    def test_cancellation_only_for_a_previously_delivered_event(self):
        v=event(event_status='cancelled');self.save(v);self.assertEqual(self.pending(),[])
        v=event();self.shown(v);v.event_status='cancelled';v.active=False;self.save(v)
        rows=self.pending();self.assertEqual(len(rows),1)
        p=json.loads(rows[0]['payload']);self.assertEqual(p['_notification_kind'],'event_cancelled')
        text=format_message(p);self.assertIn('Мероприятие отменено',text);self.assertNotIn('регистрация у организатора',text)
        # Corrections to previously shown events survive changed search filters.
        c=deepcopy(self.config);c['filters']|={'directions':['mlops'],'event_price':'free','event_languages':['ru']}
        self.store.refilter(c);self.assertEqual(len(self.pending()),1)

    def test_cancellation_retry_restart_and_no_repeated_notice(self):
        v=self.shown();v.event_status='cancelled';self.save(v)
        telegram=Mock();telegram.send.side_effect=FetchError('offline')
        monitor=Monitor(self.config,self.store,telegram,delivery_delay=0)
        with self.assertRaises(FetchError):monitor.dispatch_pending()
        self.assertEqual(len(self.pending()),1)
        self.store.close();self.store=Store(self.path);telegram=Mock()
        self.assertEqual(Monitor(self.config,self.store,telegram,delivery_delay=0).dispatch_pending()['vacancies'],1)
        self.save(v);self.store.refilter(self.config);self.assertEqual(self.pending(),[])
        self.assertEqual(telegram.send.call_count,1)

    def test_global_switch_pause_and_watch_expiry_suppress_corrections(self):
        v=self.shown();v.event_status='cancelled';self.save(v)
        c=deepcopy(self.config);c['filters']['event_cancellations']=False;self.store.refilter(c);self.assertEqual(self.pending(),[])
        self.store.refilter(self.config);self.assertEqual(len(self.pending()),1)
        c=deepcopy(self.config);c['monitor']['paused']=True;telegram=Mock()
        self.assertEqual(Monitor(c,self.store,telegram,delivery_delay=0).dispatch_pending()['vacancies'],0)
        with patch('botwork.events.event_now',return_value=NOW+timedelta(days=5)):
            self.store.refilter(self.config);self.assertEqual(self.pending(),[]);self.assertEqual(self.store.watched_events('ctftime'),[])

    def test_legacy_event_payload_gets_watch_dates_without_resending(self):
        v=self.shown();row=self.store.db.execute('SELECT * FROM vacancies').fetchone();p=json.loads(row['payload'])
        for key in ['_event_watch_until','_event_cancel_notified']:p.pop(key,None)
        for key in ['language_codes','price_kind','event_check_error']:p['vacancy'].pop(key,None)
        with self.store.db:self.store.db.execute('UPDATE vacancies SET payload=?',(json.dumps(p),))
        self.save(v);self.assertEqual(self.pending(),[]);self.assertEqual(len(self.store.watched_events('ctftime')),1)
        v.event_status='cancelled';self.save(v);self.assertEqual(len(self.pending()),1)

    def test_ods_explicit_restoration_and_withdrawn_unsent_cancellation(self):
        v=event(source='ods_events',external_id=ods()['id'],url='https://ods.ai/events/meetup-1')
        self.shown(v);v.event_status='cancelled';self.save(v)
        v.event_status='scheduled';self.save(v);self.assertEqual(self.pending('ods_events'),[])
        v.event_status='cancelled';self.save(v);self.store.mark_sent(self.pending('ods_events')[0])
        v.event_status='scheduled';self.save(v)
        row=self.pending('ods_events')[0];self.assertEqual(json.loads(row['payload'])['_notification_kind'],'event_restored')
        self.store.mark_sent(row);self.save(v);self.assertEqual(self.pending('ods_events'),[])
        v.event_status='cancelled';self.save(v);self.assertEqual(len(self.pending('ods_events')),1)

    def test_ctftime_uses_only_explicit_title_markers(self):
        for title in ['[Cancelled] Example CTF','Canceled: Example CTF','Example CTF (cancelled)','Отменено: CTF']:
            self.assertEqual(parse_ctftime(json.dumps([ctf(title=title)]))[0].event_status,'cancelled')
        self.assertEqual(parse_ctftime(json.dumps([ctf(description='If an event is cancelled, contact organizers')]))[0].event_status,'scheduled')

    def test_missing_ctf_is_rechecked_and_404_is_not_a_cancellation(self):
        previous=event()
        client=Mock();client.get.side_effect=['[]',json.dumps(ctf(title='[Cancelled] Practice CTF'))]
        rows=collect_ctftime({'url':'https://ctftime.org/api/v1/events/','_watched_events':[previous.to_dict()]},client,self.rules)
        self.assertEqual(rows[0].event_status,'cancelled');self.assertTrue(client.get.call_args.args[0].endswith('/1/'))
        client.get.side_effect=['[]',FetchError('HTTP 404')]
        rows=collect_ctftime({'url':'https://ctftime.org/api/v1/events/','_watched_events':[previous.to_dict()]},client,self.rules)
        self.assertEqual(rows[0].event_status,'scheduled');self.assertTrue(rows[0].event_check_error)
        self.assertFalse(decide_event(rows[0],self.rules).matched)

    def test_missing_ods_event_reads_structured_cancelled_status(self):
        raw=ods(status='cancelled');raw|={'content':[],'registration':None}
        previous=event(source='ods_events',external_id=raw['id'],url='https://ods.ai/events/meetup-1')
        client=Mock();client.get.side_effect=[json.dumps(dict(events=[],total=0,offset=0,limit=20)), '<script id="__NEXT_DATA__">'+json.dumps({'props':{'pageProps':{'event':raw}}})+'</script>']
        rows=collect_ods({'_watched_events':[previous.to_dict()]},client,self.rules)
        self.assertEqual(rows[0].event_status,'cancelled')
        client.get.side_effect=[json.dumps(dict(events=[],total=0,offset=0,limit=20)),FetchError('HTTP 404')]
        rows=collect_ods({'_watched_events':[previous.to_dict()]},client,self.rules)
        self.assertTrue(rows[0].event_check_error);self.assertEqual(rows[0].event_status,'scheduled')

    def test_strict_price_filter_still_reads_ods_own_details(self):
        raw=ods();detail=raw|{'content':[{'type':'html','content':'<p>Стоимость: бесплатно</p><p>Язык: Русский</p>'}],'registration':None}
        client=Mock();client.get.side_effect=[json.dumps(dict(events=[raw],total=1,offset=0,limit=20)), '<script id="__NEXT_DATA__">'+json.dumps({'props':{'pageProps':{'event':detail}}})+'</script>']
        rules=self.rules|{'event_price':'free','event_languages':['ru']}
        v=collect_ods({},client,rules)[0];self.assertTrue(decide_event(v,rules).matched)

    def test_cancelled_card_in_digest_retains_warning_and_is_marked_once(self):
        v=self.shown();v.event_status='cancelled';self.save(v);telegram=Mock()
        self.assertEqual(deliver_digest(self.store,telegram,['ctftime'],20,delay=0),1)
        self.assertIn('Мероприятие отменено',telegram.send.call_args.args[0]);self.assertEqual(self.pending(),[])

    def test_background_snapshot_includes_watch_data_without_sqlite_in_worker(self):
        self.shown();monitor=Monitor(self.config,self.store,Mock(),delivery_delay=0)
        with patch('botwork.background.BackgroundScan') as worker:
            self.assertTrue(monitor.request_check())
            snapshot=worker.call_args.args[0];self.assertEqual(len(snapshot['sources']['ctftime']['_watched_events']),1)
            self.assertNotIn('_watched_events',monitor.config['sources']['ctftime'])
            monitor.close()

    def test_watch_failure_status_then_positive_cancellation_through_full_scan(self):
        from botwork.service import scan
        from botwork.reporting import status_text
        self.shown();c=deepcopy(self.config)
        for name,source in c['sources'].items():source['enabled']=name=='ctftime'
        client=Mock();client.get.side_effect=['[]',FetchError('HTTP 404')]
        result=scan(c,self.store,client)
        self.assertIsNone(result['sources']['ctftime']['error'])
        self.assertEqual(result['sources']['ctftime']['watch_errors'],1)
        self.assertIn('Не удалось перепроверить ранее присланных событий: 1',status_text(self.store,c))
        self.assertEqual(self.pending(),[])
        client.get.side_effect=['[]',json.dumps(ctf(title='[Cancelled] Practice CTF'))]
        scan(c,self.store,client)
        self.assertEqual(len(self.pending()),1)
        telegram=Mock();self.assertEqual(Monitor(c,self.store,telegram,delivery_delay=0).dispatch_pending()['vacancies'],1)
        self.assertIn('Мероприятие отменено',telegram.send.call_args.args[0])

    def test_positive_cancel_with_removed_dates_uses_previously_shown_dates(self):
        previous=event();client=Mock()
        client.get.side_effect=['[]',json.dumps(ctf(title='[Cancelled] Practice CTF',start=None,finish=None))]
        current=collect_ctftime({'url':'https://ctftime.org/api/v1/events/','_watched_events':[previous.to_dict()]},client,self.rules)[0]
        self.assertEqual(current.event_status,'cancelled');self.assertEqual(current.end_at,previous.end_at)
        self.assertTrue(current.notes);self.assertFalse(current.event_check_error)
        raw=ods(status='cancelled',start_dt=None,end_dt=None)
        previous=event(source='ods_events',external_id=raw['id'],url='https://ods.ai/events/meetup-1')
        client.get.side_effect=[json.dumps(dict(events=[],total=0,offset=0,limit=20)), '<script id="__NEXT_DATA__">'+json.dumps({'props':{'pageProps':{'event':raw}}})+'</script>']
        current=collect_ods({'_watched_events':[previous.to_dict()]},client,self.rules)[0]
        self.assertEqual(current.event_status,'cancelled');self.assertEqual(current.end_at,previous.end_at)

if __name__=='__main__':unittest.main()
