from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from workbot.config import load_config, validate_config
from workbot.models import Vacancy
from workbot.matching import decide
from workbot.directions import source_settings, collection_rules
from workbot.settings import Settings, effective_config
from workbot.storage import Store
from workbot.new_sources import collect_habr, habr_page
from tests.test_new_sources import habr_html, habr_row
import json

ROOT = Path(__file__).resolve().parents[1]

class ProfilesTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config(str(ROOT/'config.toml'))
        self.rules = self.config['filters']

    def v(self, title, **kwargs):
        return Vacancy('habr','1',title,'Компания','https://example.com',work_format='remote',**kwargs)

    def test_groups_are_independently_selectable(self):
        cases = [('security','Junior SOC'),('devsecops','DevSecOps intern'),('devops','Junior SRE'),('devops','Platform Engineer intern'),('mlops','MLOps intern'),('mlops','Junior Data Engineer')]
        for group,title in cases:
            rules = self.rules | {'directions':[group]}
            self.assertTrue(decide(self.v(title),rules).matched, title)
            for other in ('devops','mlops'):
                if other != group:
                    self.assertFalse(decide(self.v(title),self.rules | {'directions':[other]}).matched,title)

    def test_starting_level_and_geography_remain_required(self):
        for title in ['Senior DevOps','Middle MLOps','Platform Engineer','Data Engineer']:
            self.assertFalse(decide(self.v(title),self.rules).matched)
        self.assertFalse(decide(Vacancy('habr','1','Junior DevOps','C','https://e.test',cities=['Москва'],work_format='hybrid'),self.rules).matched)
        self.assertFalse(decide(self.v('Junior Python',description='Наша компания занимается MLOps и DevOps'),self.rules).matched)

    def test_old_rules_without_directions_keep_security_default(self):
        rules=deepcopy(self.rules);rules.pop('directions')
        self.assertFalse(decide(self.v('Junior DevOps'),rules).matched)
        self.assertTrue(decide(self.v('Junior SOC'),rules).matched)

    def test_shared_data_science_category_does_not_establish_data_engineering(self):
        categories=['IT: Data Science и Data Engineering']
        self.assertFalse(decide(self.v('Data Science intern',categories=categories),self.rules).matched)
        self.assertTrue(decide(self.v('Data Engineer intern',categories=categories),self.rules).matched)

    def test_sber_russian_fulltext_inflection_does_not_bypass_role_filter(self):
        from workbot.sber_source import collect_sber
        from tests.test_sber_source import vacancy, page, dictionary, CATEGORIES, EXPERIENCES, SCHEDULES
        class Client:
            def get(self,url):
                if url.endswith('/profAreas'):return dictionary(CATEGORIES)
                if url.endswith('/experiences'):return dictionary(EXPERIENCES)
                if url.endswith('/schedules'):return dictionary(SCHEDULES)
                return page([vacancy(title='Junior Python',requirements='Работа с данными')])
        rules=self.rules|{'directions':['mlops']}
        rows=collect_sber(self.config['sources']['sber']|{'search_terms':['данных']},Client(),collection_rules(rules))
        self.assertEqual(len(rows),1);self.assertFalse(decide(rows[0],rules).matched)

    def test_sber_repairs_pagination_only_after_full_unique_count_is_proven(self):
        from workbot.sber_source import collect_sber
        from tests.test_sber_source import vacancy, page, dictionary, CATEGORIES, EXPERIENCES, SCHEDULES
        from urllib.parse import parse_qs, urlsplit
        from workbot.sources import ParseError
        class Client:
            rounds=0
            def get(self,url):
                if url.endswith('/profAreas'):return dictionary(CATEGORIES)
                if url.endswith('/experiences'):return dictionary(EXPERIENCES)
                if url.endswith('/schedules'):return dictionary(SCHEDULES)
                offset=int(parse_qs(urlsplit(url).query)['skip'][0])
                if offset==0:self.rounds+=1
                identifier=1 if offset==1 and self.rounds==1 else offset+1
                return page([vacancy(identifier)],total=3)
        settings=self.config['sources']['sber']|{'search_terms':[],'page_size':1}
        rules=self.rules|{'directions':['security']}
        client=Client();self.assertEqual(len(collect_sber(settings,client,rules)),3);self.assertEqual(client.rounds,2)
        class AlwaysIncomplete(Client):
            def get(self,url):
                if '/publications?' in url:return page([vacancy(1)],total=3)
                return super().get(url)
        with self.assertRaises(ParseError):collect_sber(settings,AlwaysIncomplete(),rules)

    def test_search_queries_follow_groups_and_do_not_mutate_baseline(self):
        rules=self.rules | {'directions':['mlops']}
        for source in ('mts','sber','hh'):
            baseline=deepcopy(self.config['sources'][source])
            expanded=source_settings(source,baseline,rules)
            self.assertIn('MLOps',expanded['search_terms'])
            self.assertIn('Data Engineer' if source == 'hh' else 'Data',expanded['search_terms'])
            self.assertNotEqual(baseline,expanded)
        self.assertIn('mlops', ' '.join(collection_rules(rules)['security_patterns']))

    def test_invalid_profiles_and_event_settings_rejected(self):
        for field,value in [('directions',[]),('directions',['sales']),('directions',[{}]),('events_enabled','on'),('event_days',0),('event_types',['vacancy']),('event_formats',['field'])]:
            config=deepcopy(self.config);config['filters'][field]=value
            with self.assertRaises(ValueError):validate_config(config)

    def test_telegram_profile_persistence_replay_and_refilter_without_resend(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=str(Path(tmp)/'db');store=Store(path)
            manager=Settings(self.config,store)
            self.assertIn('сохранены',manager.execute('/set','directions devops',1))
            profile=effective_config(self.config,store)
            self.assertEqual(profile['filters']['directions'],['devops'])
            v=self.v('Junior DevOps');store.save_snapshot('habr',[(v,decide(v,profile['filters']))])
            store.mark_sent(store.pending(['habr'],10)[0])
            manager.execute('/set','directions security',2);store.refilter(effective_config(self.config,store))
            self.assertEqual(store.pending(['habr'],10),[])
            manager.execute('/set','directions devops,mlops',3);store.refilter(effective_config(self.config,store))
            self.assertEqual(store.pending(['habr'],10),[])
            manager.execute('/set','directions devops',1)
            self.assertEqual(effective_config(self.config,store)['filters']['directions'],['devops','mlops'])
            store.close();store=Store(path)
            self.assertEqual(effective_config(self.config,store)['filters']['directions'],['devops','mlops'])
            for n,args in enumerate(['events off','event_days 30','event_formats remote','event_types ctf,webinar','event_unverified off'],10):
                self.assertIn('сохранены',Settings(self.config,store).execute('/set',args,n))
            f=effective_config(self.config,store)['filters'];self.assertFalse(f['events_enabled']);self.assertEqual(f['event_types'],['ctf','webinar'])
            store.close()

    def test_habr_query_must_be_echoed_and_all_pages_collected(self):
        def page(number,identifier):
            data=json.loads(habr_html([habr_row(identifier,title='Junior DevOps',qualification='Junior',divisions=[{'title':'DevOps'}])],page=number,total=2).split('>',1)[1].rsplit('<',1)[0])
            data['search']['query']='DevOps';data['search']['order']='date';data['search']['filters']['s']=[]
            return '<script type="application/json">'+json.dumps(data)+'</script>'
        from workbot.new_sources import collect_habr_query
        class Client:
            def get(self,url):return page(2,2) if 'page=2' in url else page(1,1)
        rows=collect_habr_query({'url':'https://career.habr.com/vacancies/spec/infosec'},Client(),self.rules,'DevOps')
        self.assertEqual(len(rows),2)
        from workbot.sources import ParseError
        with self.assertRaises(ParseError):habr_page(page(1,1),1,'MLOps')

    def test_programmes_do_not_relabel_devops_as_security(self):
        from workbot.service import scan
        from workbot.matching import contains
        recorded={}
        def collect(name,settings,client,rules):
            recorded[name]=rules
            return []
        config=deepcopy(self.config)
        for name,source in config['sources'].items():source['enabled']=name in ('habr','pt_fast_track','pt_start_training','x5_internships')
        with patch('workbot.service.collect',side_effect=collect):scan(config,client=object())
        self.assertTrue(contains(recorded['habr']['security_patterns'],'Junior DevOps'))
        for name in ('pt_fast_track','pt_start_training','x5_internships'):
            self.assertFalse(contains(recorded[name]['security_patterns'],'Junior DevOps'))

    def test_sber_known_category_alias_preserves_id_validation(self):
        from workbot.sber_source import parse_publications
        from tests.test_sber_source import vacancy, page, EXPERIENCES, SCHEDULES
        from workbot.sources import ParseError
        categories={'developer':'Информационные технологии:Программист, разработчик'}
        row=vacancy(specializationId='developer',specialization='IT: Разработка',title='Junior DevOps')
        result,_=parse_publications(page([row]),'https://rabota.sber.ru',0,100,categories,EXPERIENCES,SCHEDULES)
        self.assertEqual(result[0].categories,list(categories.values()))
        for changes in [{'specialization':'Other'},{'specializationId':'not-selected'}]:
            with self.assertRaises(ParseError):parse_publications(page([row|changes]),'https://rabota.sber.ru',0,100,categories,EXPERIENCES,SCHEDULES)

    def test_sber_unassigned_category_is_usable_only_in_fulltext_search(self):
        from workbot.sber_source import parse_publications
        from tests.test_sber_source import vacancy, page, EXPERIENCES, SCHEDULES
        from workbot.sources import ParseError
        row=vacancy(title='Junior Python',specializationId='new-id',specialization=None)
        with self.assertRaises(ParseError):parse_publications(page([row]),'https://rabota.sber.ru',0,100,{},EXPERIENCES,SCHEDULES)
        rows,_=parse_publications(page([row]),'https://rabota.sber.ru',0,100,{},EXPERIENCES,SCHEDULES,allow_unknown_category=True)
        self.assertEqual(rows[0].categories,[])
        self.assertFalse(decide(rows[0],self.rules).matched)
        self.assertTrue(rows[0].notes)
        with self.assertRaises(ParseError):parse_publications(page([row|{'specialization':'Информационная безопасность'}]),'https://rabota.sber.ru',0,100,{},EXPERIENCES,SCHEDULES,allow_unknown_category=True)

    def test_mts_body_search_is_verified_without_using_it_as_a_role(self):
        from workbot.mts_source import collect_mts
        from tests.test_mts_source import row, page
        class Client:
            calls=[]
            def get(self,url):
                self.calls.append(url)
                if '/api/' in url:return page([row(title='Junior Python')])
                return '<h1 class="vacancy__header__title">Junior Python</h1><div class="vacancy__content"><div class="vacancy__data">Разработка сервисов для инфраструктуры</div></div>'
        client=Client()
        rows=collect_mts(self.config['sources']['mts']|{'page_size':1,'search_terms':['инфраструктур']},client,collection_rules(self.rules))
        self.assertEqual(len(rows),1);self.assertFalse(decide(rows[0],self.rules).matched)
        self.assertEqual(len(client.calls),2)

if __name__=='__main__':unittest.main()
