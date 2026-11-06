from copy import deepcopy
from datetime import datetime, timezone, timedelta
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs,urlsplit
from workbot.config import load_config
from workbot.events import decide_event, parse_ctftime, collect_ctftime, collect_ods
from workbot.models import Vacancy
from workbot.sources import ParseError
from workbot.storage import Store
from workbot.telegram import format_message, deliver_digest
from workbot.runner import Monitor

ROOT=Path(__file__).resolve().parents[1]
NOW=datetime(2030,1,1,tzinfo=timezone.utc)
START=(NOW+timedelta(days=2)).isoformat();END=(NOW+timedelta(days=3)).isoformat()

def ctf(identifier=1,**kwargs):
    return dict(id=identifier,title='Practice CTF',ctftime_url=f'https://ctftime.org/event/{identifier}/',start=START,finish=END,onsite=False,organizers=[{'name':'Club'}],description='Tasks',location='',restrictions='Open',format='Jeopardy',url='https://example.org')|kwargs

def ods(identifier=1,**kwargs):
    return dict(id=f'00000000-0000-0000-0000-{identifier:012d}',slug=f'meetup-{identifier}',title='MLOps meetup',description='Практика',location='Онлайн',start_dt=START,end_dt=END,status='announced',related_hubs=[{'title':'MLOps и Data Engineering'}])|kwargs

def event(**kwargs):
    v= parse_ctftime(json.dumps([ctf()]))[0]
    for k,value in kwargs.items():setattr(v,k,value)
    return v

class EventTests(unittest.TestCase):
    def setUp(self):
        self.config=load_config(str(ROOT/'config.toml'));self.rules=self.config['filters']
        self.clock=patch('workbot.events.event_now',return_value=NOW);self.clock.start();self.addCleanup(self.clock.stop)

    def test_event_does_not_require_junior_but_does_require_direction(self):
        self.assertTrue(decide_event(event(),self.rules).matched)
        self.assertFalse(decide_event(event(),self.rules|{'directions':['mlops']}).matched)
        self.assertFalse(decide_event(event(),self.rules|{'events_enabled':False}).matched)

    def test_location_format_types_and_independent_job_formats(self):
        v=event(work_format='office',cities=['Москва'])
        self.assertFalse(decide_event(v,self.rules).matched)
        v.cities=['Санкт-Петербург'];self.assertTrue(decide_event(v,self.rules|{'allowed_formats':['remote']}).matched)
        self.assertFalse(decide_event(v,self.rules|{'event_formats':['remote']}).matched)
        self.assertFalse(decide_event(event(),self.rules|{'event_types':['meetup']}).matched)
        self.assertFalse(decide_event(event(work_format='unknown',cities=[]),self.rules|{'include_unknown_location':False}).matched)

    def test_registration_and_deadlines_are_not_assumed(self):
        d=decide_event(event(),self.rules);self.assertTrue(d.matched);self.assertTrue(d.warnings)
        self.assertFalse(decide_event(event(),self.rules|{'event_unverified':False}).matched)
        for changes in [dict(recruitment_status='closed'),dict(event_status='cancelled'),dict(end_at=NOW.isoformat()),dict(start_at=(NOW+timedelta(days=120)).isoformat()),dict(registration_deadline=(NOW-timedelta(hours=1)).isoformat()),dict(start_at='2030-01-01')]:
            self.assertFalse(decide_event(event(**changes),self.rules).matched,changes)

    def test_card_preserves_explicit_unknowns_and_moscow_timezone(self):
        v=event();message=format_message({'vacancy':v.to_dict(),'decision':decide_event(v,self.rules).__dict__})
        for text in ['МСК','Регистрация: <b>Не подтверждена','Дедлайн регистрации: не указан','Стоимость: не указана','Язык: не указан','03.01.2030 03:00']:
            self.assertIn(text,message)
        self.assertNotIn('Зарплата',message)

    def test_ctftime_rejects_bad_dates_links_duplicates_and_shape(self):
        for text in ['{}',json.dumps([ctf(start='2030-01-03')]),json.dumps([ctf(ctftime_url='https://bad.test')]),json.dumps([ctf(),ctf()]),json.dumps([ctf(onsite='no')])]:
            with self.assertRaises(ParseError):parse_ctftime(text)

    def test_ctftime_splits_a_possibly_truncated_window(self):
        client=Mock();client.get.side_effect=[json.dumps([ctf(i) for i in range(1,101)]),json.dumps([ctf(1)]),json.dumps([ctf(1),ctf(2)])]
        rows=collect_ctftime({'url':'https://ctftime.org/api/v1/events/'},client,self.rules)
        self.assertEqual(len(rows),2);self.assertEqual(client.get.call_count,3)
        with self.assertRaises(ParseError):collect_ctftime({'url':'https://bad.test'},client,self.rules)

    def test_ods_complete_pagination_and_duplicate_drift_rejection(self):
        raws=[ods(i,location='OFFLINE, Москва') for i in range(1,22)]
        class Client:
            def get(self,url):
                offset=int(parse_qs(urlsplit(url).query)['offset'][0])
                return json.dumps(dict(events=raws[offset:offset+20],limit=20,offset=offset,total=21))
        self.assertEqual(len(collect_ods({},Client(),self.rules)),21)
        bad=Mock();bad.get.return_value=json.dumps(dict(events=raws[:1],limit=20,offset=0,total=21))
        with self.assertRaises(ParseError):collect_ods({},bad,self.rules)

    def test_ods_registration_comes_from_own_card_and_external_form_is_unknown(self):
        raw=ods();detail=deepcopy(raw)|{'registration':{'is_completed':False,'is_limit_exceed':False},'content':[]}
        class Client:
            def get(self,url):
                if '/list/' in url:return json.dumps(dict(events=[raw],limit=20,offset=0,total=1))
                return '<script id="__NEXT_DATA__" type="application/json">'+json.dumps({'props':{'pageProps':{'event':detail}}})+'</script>'
        self.assertEqual(collect_ods({},Client(),self.rules)[0].recruitment_status,'unknown')
        detail['content']=[{'type':'html','content':'Регистрация открыта'}]
        self.assertEqual(collect_ods({},Client(),self.rules|{'event_unverified':False})[0].recruitment_status,'open')
        detail['content']=[{'type':'html','content':'<a href="https://registration.example/form">Зарегистрироваться</a>'}]
        v=collect_ods({},Client(),self.rules)[0]
        self.assertEqual(v.recruitment_status,'unknown');self.assertEqual(v.application_url,'https://registration.example/form')
        self.assertTrue(v.notes)
        detail['id']='different'
        with self.assertRaises(ParseError):collect_ods({},Client(),self.rules)

    def test_saved_event_once_then_date_update_then_registration_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(str(Path(tmp)/'db'));v=event()
            def save():store.save_snapshot('ctftime',[(v,decide_event(v,self.rules))])
            save();row=store.pending(['ctftime'],20)[0];store.mark_sent(row);save();self.assertEqual(store.pending(['ctftime'],20),[])
            v.start_at=(NOW+timedelta(days=2,hours=1)).isoformat();save()
            row=store.pending(['ctftime'],20)[0];self.assertEqual(json.loads(row['payload'])['_notification_kind'],'event_update')
            store.refilter(self.config);self.assertEqual(len(store.pending(['ctftime'],20)),1)
            store.mark_sent(row);save();self.assertEqual(store.pending(['ctftime'],20),[])
            v.recruitment_status='open';save();row=store.pending(['ctftime'],20)[0];store.mark_sent(row)
            v.recruitment_status='unknown';save();self.assertEqual(store.pending(['ctftime'],20),[])
            v.recruitment_status='open';save();self.assertEqual(store.pending(['ctftime'],20),[]);store.close()

    def test_expired_pending_event_is_not_delivered_and_digest_supports_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(str(Path(tmp)/'db'));v=event()
            store.save_snapshot('ctftime',[(v,decide_event(v,self.rules))]);telegram=Mock()
            self.assertEqual(deliver_digest(store,telegram,['ctftime'],20,delay=0),1)
            self.assertIn('Дедлайн регистрации',telegram.send.call_args.args[0]);store.close()
            store=Store(str(Path(tmp)/'db'));v.external_id='2';store.save_snapshot('ctftime',[(v,decide_event(v,self.rules))])
            monitor=Monitor(self.config,store,telegram,delivery_delay=0)
            with patch('workbot.events.event_now',return_value=NOW+timedelta(days=4)):
                self.assertEqual(monitor.dispatch_pending()['vacancies'],0)
            store.close()

if __name__=='__main__':unittest.main()
