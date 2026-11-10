from copy import deepcopy
import json
from pathlib import Path
import unittest
from workbot.config import load_config
from workbot.new_sources import habr_page,collect_habr,yandex_page,collect_yandex_jobs
from workbot.matching import decide
from workbot.sources import ParseError

ROOT=Path(__file__).resolve().parents[1]

def habr_row(identifier=1,**changes):
    return dict(id=identifier,title='Стажёр SOC',href='/vacancies/'+str(identifier),remoteWork=True,archived=False,hidden=False,
        company={'title':'Компания'},locations=None,divisions=[{'title':'Специалист по информационной безопасности'}],
        qualification='Intern',salary={'formatted':''})|changes

def habr_html(rows=None,total=1,page=1):
    d={'search':{'filters':{'s':[174]}},'vacancies':{'list':[habr_row()] if rows is None else rows,
        'meta':{'totalResults':total,'perPage':1,'currentPage':page,'totalPages':total}}}
    return '<script type="application/json">'+json.dumps(d)+'</script>'

def ya_row(identifier=1,**changes):
    return dict(id=identifier,title='Стажёр SOC',publication_slug_url='intern-soc-'+str(identifier),status='published',
        vacancy={'cities':[{'name':'Санкт-Петербург'}],'work_modes':[{'slug':'mixed','name':'Гибридный'}]},
        public_service={'name':'Информационная безопасность'},short_summary='Стажировка')|changes

def ya_page(rows=None,total=1,next_url=None):
    d={'count':total,'results':[ya_row()] if rows is None else rows}
    if next_url is not None:d['next']=next_url
    return json.dumps(d)

class NewSourceTests(unittest.TestCase):
    def setUp(self):self.config=load_config(str(ROOT/'config.toml'));self.rules=self.config['filters'];self.rules['directions']=['security']
    def test_habr_metadata_and_unknown_location(self):
        row=habr_page(habr_html(),1)[0][0]
        self.assertTrue(decide(row,self.rules).matched);self.assertEqual(row.cities,[])
        self.assertEqual(row.work_format,'remote');self.assertEqual(row.salary,'не указана')
    def test_habr_middle_not_junior_even_with_intern_text(self):
        row=habr_page(habr_html([habr_row(qualification='Middle')]),1)[0][0]
        self.assertTrue(decide(row,self.rules).matched)
    def test_habr_empty_incomplete_and_repeated(self):
        self.assertEqual(habr_page(habr_html([],total=0),1)[0],[])
        for html in [habr_html([],total=1),habr_html(page=2),'<html>error</html>',habr_html([habr_row(),habr_row()],total=2)]:
            with self.assertRaises(ParseError):habr_page(html,1)
    def test_habr_detail_is_scoped(self):
        settings=self.config['sources']['habr'];self.rules['include_words']=[]
        class Client:
            def get(self,url):
                return habr_html() if url==settings['url'] else '<h1>Стажёр SOC</h1><div class="vacancy-description__text">Анализ SIEM</div><aside>Senior, гибрид</aside>'
        row=collect_habr(settings,Client(),self.rules)[0]
        self.assertIn('SIEM',row.description);self.assertNotIn('Senior',row.description)
    def test_habr_later_page_count_change_error(self):
        settings=self.config['sources']['habr'];self.rules['include_words']=[]
        class Client:
            def get(self,url):return habr_html(total=2) if '?' not in url else habr_html([habr_row(2)],total=3,page=2)
        with self.assertRaises(ParseError):collect_habr(settings,Client(),self.rules)
    def test_yandex_last_page_without_next(self):
        rows,total,next_url=yandex_page(ya_page());self.assertEqual(total,1);self.assertIsNone(next_url)
        self.assertTrue(decide(rows[0],self.rules).matched)
    def test_yandex_invalid_card_and_duplicate(self):
        for value in [ya_page([ya_row(status='new')]),ya_page([ya_row(publication_slug_url='https://example.com')]),ya_page([ya_row(),ya_row()],total=2),'<html>error</html>']:
            with self.assertRaises(ParseError):yandex_page(value)
    def test_yandex_cursor_and_own_sections(self):
        settings=self.config['sources']['yandex_jobs']
        detail='<h1>Стажёр SOC</h1><div><h2>Какие задачи вас ждут</h2>Анализ SIEM</div><div><h2>Мы ждём, что вы</h2>Основы ОС</div><div><h2>Бенефиты</h2>Senior</div>'
        class Client:
            def get(self,url):
                if url==settings['api_url']:return ya_page(total=2,next_url='/jobs/api/jobs/publications/?cursor=2')
                if 'cursor=2' in url:return ya_page([ya_row(2)],total=2)
                return detail
        rows=collect_yandex_jobs(settings,Client(),self.rules)
        self.assertEqual(len(rows),2);self.assertNotIn('Бенефиты',rows[0].description)
    def test_yandex_cursor_host_rejected_before_fetch(self):
        settings=self.config['sources']['yandex_jobs'];calls=[]
        class Client:
            def get(self,url):calls.append(url);return ya_page(total=2,next_url='https://example.com/jobs/api/jobs/publications/')
        with self.assertRaises(ParseError):collect_yandex_jobs(settings,Client(),self.rules)
        self.assertEqual(len(calls),1)
    def test_yandex_truncated_total_and_page_limit(self):
        settings=self.config['sources']['yandex_jobs']
        class Client:
            def get(self,url):return ya_page(total=2)
        with self.assertRaises(ParseError):collect_yandex_jobs(settings,Client(),self.rules)

if __name__=='__main__':unittest.main()
