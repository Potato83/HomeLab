from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request

from botwork.config import load_config
from botwork.hh_source import collect_hh, parse_page, parse_vacancy, salary_text, security_roles
from botwork.http import FetchError, HttpClient, NoRedirect
from botwork.matching import decide
from botwork.runner import Monitor
from botwork.service import scan
from botwork.sources import ParseError
from botwork.storage import Store

ROOT = Path(__file__).resolve().parents[1]
ROLE = 'Специалист по информационной безопасности'
ROLES = json.dumps({'categories': [{'id': '11', 'roles': [{'id': '113', 'name': ROLE}]}]})


def vacancy(identifier='123', **changes):
    return dict(id=identifier, name='Стажёр SOC', employer={'name': 'Тестовая компания'},
        archived=False, closed_for_applicants=False, area={'name': 'Санкт-Петербург'},
        experience={'id': 'noExperience', 'name': 'Нет опыта'},
        professional_roles=[{'id': '113', 'name': ROLE}],
        work_format=[{'id': 'HYBRID', 'name': 'Гибрид'}],
        description='<p>Мониторинг событий ИБ</p>', salary=None) | changes


def page(rows=None, number=0, total=None, size=1):
    rows = [vacancy()] if rows is None else rows
    total = len(rows) if total is None else total
    return json.dumps({'items': rows, 'page': number, 'pages': (total + size - 1) // size,
        'per_page': size, 'found': total})


class Client:
    def __init__(self, pages=None, searches=None):
        self.calls = []
        self.pages = pages or {0: page()}
        self.searches = searches or {}
        self.details = {}

    def get(self, url, *, headers=None, follow_redirects=True):
        self.calls.append((url, headers, follow_redirects))
        if headers.get('Authorization') != 'Bearer test-token' or follow_redirects:
            raise AssertionError('Authentication or redirect policy missing')
        path, query = urlsplit(url).path, parse_qs(urlsplit(url).query)
        if path == '/professional_roles':
            return ROLES
        if path == '/vacancies':
            if query['experience'] != ['noExperience', 'between1And3']:
                raise AssertionError('Experience filter missing')
            number = int(query['page'][0])
            return self.searches[query['text'][0]][number] if 'text' in query else self.pages[number]
        identifier = path.rsplit('/', 1)[-1]
        return json.dumps(self.details.get(identifier, vacancy(identifier)))


class HHTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config(str(ROOT / 'config.toml'))
        self.config['filters']['directions'] = ['security']  # Legacy security snapshot fixtures.
        self.rules = self.config['filters']
        self.settings = self.config['sources']['hh'] | {'enabled': True, 'search_terms': [], 'page_size': 1}
        self.env = patch.dict('os.environ', {'HH_API_TOKEN': 'test-token'})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_intern_spb_hybrid_and_remote_any_region(self):
        self.assertTrue(decide(parse_vacancy(vacancy()), self.rules).matched)
        v = parse_vacancy(vacancy(area={'name': 'Москва'}, work_format=[{'id': 'REMOTE', 'name': 'Из дома'}]))
        self.assertTrue(decide(v, self.rules).matched)
        self.assertEqual(v.level, 'intern')
        self.assertEqual(v.url, 'https://hh.ru/vacancy/123')

    def test_moscow_hybrid_and_field_work_rejected(self):
        for work_format in [[{'id': 'HYBRID', 'name': 'Гибрид'}], [{'id': 'FIELD_WORK', 'name': 'Разъездная'}]]:
            v = parse_vacancy(vacancy(area={'name': 'Москва'}, work_format=work_format))
            self.assertFalse(decide(v, self.rules).matched)

    def test_new_format_remote_option_and_old_schedule(self):
        v = parse_vacancy(vacancy(work_format=[{'id': 'HYBRID', 'name': 'Гибрид'}, {'id': 'REMOTE', 'name': 'Из дома'}]))
        self.assertEqual(v.work_format, 'remote')
        v = parse_vacancy(vacancy(work_format=None, schedule={'id': 'remote'}))
        self.assertEqual(v.work_format, 'remote')
        v = parse_vacancy(vacancy(work_format=[], schedule={'id': 'fullDay'}))
        self.assertEqual(v.work_format, 'unknown')

    def test_archived_and_closed_not_notified(self):
        for changes in [dict(archived=True), dict(closed_for_applicants=True)]:
            self.assertFalse(decide(parse_vacancy(vacancy(**changes)), self.rules).matched)

    def test_experience_and_unknown_location(self):
        v = parse_vacancy(vacancy(experience={'id': 'between3And6', 'name': '3–6 лет'}))
        self.assertEqual(v.level, 'experienced')
        self.assertFalse(decide(v, self.rules).matched)
        v = parse_vacancy(vacancy(name='Инженер ИБ', experience={'id': 'between1And3', 'name': '1–3 года'}))
        self.assertFalse(decide(v, self.rules).matched)
        v = parse_vacancy(vacancy(area=None, work_format=None))
        self.assertTrue(decide(v, self.rules).warnings)

    def test_invalid_card_does_not_assume_metadata(self):
        for changes in [dict(id=123), dict(archived='false'), dict(name=''), dict(employer=None),
                        dict(experience={'id': 'new', 'name': 'Новое'}), dict(work_format=[{'id': 'NEW', 'name': 'Новый'}]),
                        dict(description={}), dict(professional_roles='ИБ')]:
            with self.subTest(changes=changes), self.assertRaises(ParseError):
                parse_vacancy(vacancy(**changes))

    def test_salary_period_currency_and_tax(self):
        v = vacancy(salary_range={'from': 50000, 'to': 60000, 'currency': 'RUR', 'gross': False,
            'mode': {'name': 'За месяц'}, 'frequency': {'name': 'Раз в месяц'}})
        text = salary_text(v)
        self.assertIn('50 000', text)
        self.assertIn('₽', text)
        self.assertIn('на руки', text)
        self.assertIn('За месяц', text)
        for value in [{'from': -1}, {'from': '50000'}, {'from': 90000, 'to': 80000}]:
            with self.assertRaises(ParseError):
                salary_text(vacancy(salary=value))

    def test_page_counts_duplicates_and_depth(self):
        for text in [page([], total=1), page(number=1), page([vacancy(), vacancy()], size=2),
                     page(total=2001), '{"errors":[{"type":"forbidden"}]}', '<html>error</html>']:
            with self.subTest(text=text[:100]), self.assertRaises(ParseError):
                parse_page(text, 0, 1)
        self.assertEqual(parse_page(page([], total=0), 0, 1), ([], (0, 0)))

    def test_missing_roles_is_error(self):
        for text in ['{}', '{"categories":[]}', '{"categories":[{"roles":[{"id":"1","name":"Охранник"}]}]}']:
            with self.assertRaises(ParseError):
                security_roles(text, self.rules)

    def test_all_pages_and_scoped_authentication(self):
        client = Client({0: page([vacancy('1')], total=2), 1: page([vacancy('2')], number=1, total=2)})
        result = collect_hh(self.settings, client, self.rules)
        self.assertEqual({r.external_id for r in result}, {'1', '2'})
        self.assertTrue(all(url.startswith('https://api.hh.ru/') for url, _, _ in client.calls))
        self.assertEqual(len(client.calls), 5)

    def test_search_overlap_deduplicated_and_real_detail_used(self):
        client = Client(searches={'soc': {0: page()}})
        client.details['123'] = vacancy(description='<p>Собственное описание: SIEM</p>')
        result = collect_hh(self.settings | {'search_terms': ['SOC', ' soc ']}, client, self.rules)
        self.assertEqual(len(result), 1)
        self.assertIn('SIEM', result[0].description)
        self.assertEqual(sum('/vacancies/123' in url for url, _, _ in client.calls), 1)

    def test_api_filter_and_pagination_failures(self):
        for client, settings in [
            (Client({0: page([vacancy('1')], total=2), 1: page([vacancy('1')], number=1, total=2)}), {}),
            (Client({0: page([vacancy('1')], total=2), 1: page([vacancy('2')], number=1, total=3)}), {}),
            (Client({0: page(total=2)}), {'max_pages': 1}),
            (Client({0: page([vacancy(experience={'id': 'between3And6', 'name': '3–6 лет'})])}), {}),
            (Client({0: page([vacancy(professional_roles=[{'id': '90', 'name': 'Охранник'}])])}), {})]:
            with self.assertRaises(ParseError):
                collect_hh(self.settings | settings, client, self.rules)

    def test_wrong_detail_id_and_missing_description_are_errors(self):
        for detail in [vacancy('999'), vacancy(description=None)]:
            client = Client()
            client.details['123'] = detail
            with self.assertRaises(ParseError):
                collect_hh(self.settings, client, self.rules)

    def test_missing_token_and_untrusted_api_fail_before_network(self):
        client = Mock()
        for token in ['', 'bad token', 'bad\nheader', 'секрет', 'bad\x7f']:
            with patch.dict('os.environ', {'HH_API_TOKEN': token}), self.assertRaises(FetchError):
                collect_hh(self.settings, client, self.rules)
        with self.assertRaises(ValueError):
            collect_hh(self.settings | {'api_url': 'https://example.com'}, client, self.rules)
        client.get.assert_not_called()

    def test_auth_errors_are_actionable_and_secret_is_not_echoed(self):
        for error, expected in [('HTTP 401', 'истёк'), ('HTTP 403', 'капча'), ('HTTP 429', 'лимит'),
                                ('HTTP 302 test-token', 'доступ')]:
            client = Mock()
            client.get.side_effect = FetchError(error)
            with self.assertRaises(FetchError) as raised:
                collect_hh(self.settings, client, self.rules)
            self.assertIn(expected, str(raised.exception))
            self.assertNotIn('test-token', str(raised.exception))

    def test_failed_source_keeps_previous_snapshot_and_holds_delivery(self):
        config = deepcopy(self.config)
        for name, settings in config['sources'].items():
            settings['enabled'] = name == 'hh'
        config['sources']['hh'] |= self.settings
        with tempfile.TemporaryDirectory() as directory:
            store = Store(str(Path(directory) / 'db'))
            try:
                v = parse_vacancy(vacancy())
                store.save_snapshot('hh', [(v, decide(v, self.rules))])
                bad = Mock()
                bad.get.side_effect = FetchError('HTTP 403')
                self.assertTrue(scan(config, store, bad)['sources']['hh']['error'])
                self.assertEqual(len(store.pending(['hh'], 20)), 1)
                telegram = Mock()
                Monitor(config, store, telegram, delivery_delay=0).dispatch_pending()
                telegram.send.assert_not_called()
                scan(config, store, Client())
                Monitor(config, store, telegram, delivery_delay=0).dispatch_pending()
                self.assertEqual(telegram.send.call_count, 1)
            finally:
                store.close()

    def test_enabled_config_pins_api_host(self):
        original = (ROOT / 'config.toml').read_text().replace('enabled = false', 'enabled = true')
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / 'config.toml'
            for old, new in [('https://api.hh.ru', 'https://example.com'),
                             ('max_pages = 20', 'max_pages = 101'), ('page_size = 100', 'page_size = 0')]:
                file.write_text(original.replace(old, new))
                with self.assertRaises(ValueError):
                    load_config(str(file))


class AuthenticatedHTTPTests(unittest.TestCase):
    def test_no_redirect_handler_and_safe_302_error(self):
        request = Request('https://api.hh.ru/vacancies', headers={'Authorization': 'Bearer SECRET'})
        self.assertIsNone(NoRedirect().redirect_request(request, None, 302, '', {}, 'https://example.com'))
        with patch('botwork.http.build_opener') as build:
            build.return_value.open.side_effect = HTTPError(request.full_url, 302, '', {}, BytesIO())
            with self.assertRaisesRegex(FetchError, '^HTTP 302$'):
                HttpClient(delay=0)._get_once(request.full_url, headers={'Authorization': 'Bearer SECRET'}, follow_redirects=False)
            self.assertEqual(build.return_value.open.call_count, 1)

    def test_authorization_header_is_not_shared_between_requests(self):
        response = Mock()
        response.read.side_effect = [b'{}', b'', b'{}', b'']
        response.fp = None
        response.headers.get_content_charset.return_value = 'utf-8'
        context = Mock()
        context.__enter__ = Mock(return_value=response)
        context.__exit__ = Mock(return_value=False)
        with patch('botwork.http.build_opener') as build:
            opening = build.return_value.open
            opening.return_value = context
            client = HttpClient(delay=0)
            client.get('https://api.hh.ru/test', headers={'Authorization': 'Bearer SECRET'})
            client.get('https://example.com')
            self.assertEqual(opening.call_args_list[0].args[0].get_header('Authorization'), 'Bearer SECRET')
            self.assertIsNone(opening.call_args_list[1].args[0].get_header('Authorization'))

    def test_429_not_retried_immediately(self):
        client = HttpClient(delay=0)
        with patch.object(client, '_get_once', side_effect=FetchError('HTTP 429')) as fetch:
            with self.assertRaises(FetchError):
                client.get('https://api.hh.ru/test')
            self.assertEqual(fetch.call_count, 1)


if __name__ == '__main__':
    unittest.main()
