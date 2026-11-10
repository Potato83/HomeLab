"""Additional Sber searches must expand coverage without widening eligibility."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from workbot.config import load_config
from workbot.matching import decide
from workbot.runner import Monitor
from workbot.sber_source import collect_sber
from workbot.service import scan as real_scan

def scan(config, *args, **kwargs):
    with patch('workbot.service.source_settings', side_effect=lambda n, s, r: s), patch('workbot.service.collection_rules', side_effect=lambda r: r):
        return real_scan(config, *args, **kwargs)

from workbot.sources import ParseError
from workbot.storage import Store
from tests.test_sber_source import CATEGORY, CATEGORIES, Client, dictionary, page, vacancy

ROOT = Path(__file__).resolve().parents[1]
DEVELOPER = 'Информационные технологии:Программист, разработчик'


def other_category(identifier=456, **changes):
    return vacancy(identifier, title='Junior Security Engineer', specializationId='dev', specialization=DEVELOPER,
        requirements='Навыки security testing') | changes


class SearchClient(Client):
    def __init__(self, searches=None, pages=None):
        super().__init__(pages)
        self.searches = searches or {}

    def get(self, url):
        path = urlsplit(url).path
        query = parse_qs(urlsplit(url).query)
        if path.endswith('/profAreas'):
            self.calls.append(url)
            return dictionary(CATEGORIES | {'dev': DEVELOPER})
        if 'searchString' in query:
            self.calls.append(url)
            if 'profAreas' in query:
                raise AssertionError('Search must include other categories')
            return self.searches[query['searchString'][0]][int(query['skip'][0])]
        return super().get(url)


class SberSearchTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config(str(ROOT / 'config.toml'))
        self.config['filters']['directions'] = ['security']  # Legacy security snapshot fixtures.
        self.rules = self.config['filters']
        self.rules['security_patterns'] = [r'информационн.*безопасност']
        self.settings = self.config['sources']['sber'] | {'search_terms': ['security'], 'page_size': 1}

    def collect(self, client, **settings):
        return collect_sber(self.settings | settings, client, self.rules)

    def test_other_category_ib_title_is_found_and_matches(self):
        client = SearchClient({'security': {0: page([other_category()])}})
        rows = self.collect(client)
        self.assertEqual([row.external_id for row in rows], ['123', '456'])
        self.assertTrue(decide(rows[1], self.rules).matched)
        self.assertEqual(rows[1].categories, [DEVELOPER])

    def test_body_only_search_hit_does_not_make_developer_ib(self):
        client = SearchClient({'security': {0: page([other_category(title='Стажер Python-разработчик')])}})
        row = self.collect(client)[1]
        self.assertIn('security', row.description)
        self.assertTrue(decide(row, self.rules).matched)

    def test_overlap_with_categories_and_between_terms_is_one_record(self):
        common = vacancy(requirements='Security и SOC')
        client = SearchClient({'security': {0: page([common])}, 'soc': {0: page([common])}},
            pages={0: page([common])})
        rows = self.collect(client, search_terms=['security', 'soc'])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].external_id, '123')

    def test_equivalent_terms_are_queried_once(self):
        client = SearchClient({'soc': {0: page()}})
        self.collect(client, search_terms=[' SOC ', 'soc', 'SoC'])
        searches = [url for url in client.calls if 'searchString=' in url]
        self.assertEqual(len(searches), 1)
        self.assertEqual(parse_qs(urlsplit(searches[0]).query)['searchString'], ['soc'])

    def test_category_only_old_config_remains_compatible(self):
        client = SearchClient()
        settings = self.settings.copy()
        settings.pop('search_terms')
        rows = collect_sber(settings, client, self.rules)
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(client.calls), 4)
        self.assertFalse(any('searchString=' in url for url in client.calls))

    def test_empty_keyword_result_does_not_remove_category_hit(self):
        client = SearchClient({'security': {0: page([])}})
        self.assertEqual([r.external_id for r in self.collect(client)], ['123'])

    def test_keywords_work_when_ib_category_has_no_publications(self):
        client = SearchClient({'security': {0: page([other_category()])}}, pages={0: page([])})
        rows = self.collect(client)
        self.assertEqual([r.external_id for r in rows], ['456'])

    def test_independent_keyword_pagination(self):
        client = SearchClient({'security': {
            0: page([other_category(456)], total=2),
            1: page([other_category(789)], total=2)}})
        rows = self.collect(client)
        self.assertEqual({r.external_id for r in rows}, {'123', '456', '789'})
        queries = [parse_qs(urlsplit(url).query) for url in client.calls if 'searchString=' in url]
        self.assertEqual([q['skip'] for q in queries], [['0'], ['1']])

    def test_keyword_without_evidence_is_error(self):
        client = SearchClient({'security': {0: page()}})
        with self.assertRaisesRegex(ParseError, 'без поискового слова'):
            self.collect(client)

    def test_keyword_page_limit_is_enforced(self):
        client = SearchClient({'security': {0: page([other_category()], total=2)}})
        with self.assertRaisesRegex(ParseError, 'max_pages'):
            self.collect(client, max_pages=1)

    def test_keyword_repeat_page_and_changed_total_fail(self):
        for second in [page([other_category(456)], total=2), page([other_category(789)], total=3)]:
            with self.subTest(second=second), self.assertRaises(ParseError):
                self.collect(SearchClient({'security': {0: page([other_category(456)], total=2), 1: second}}))

    def test_conflicting_same_id_between_queries_is_error(self):
        common = vacancy(requirements='Security')
        for changes in [dict(title='Junior Security Engineer'), dict(conditions='Работа в офисе в Москве'),
                        dict(salary_min=90000)]:
            with self.subTest(changes=changes), self.assertRaisesRegex(ParseError, 'противоречивые версии'):
                self.collect(SearchClient({'security': {0: page([common | changes])}}, pages={0: page([common])}))

    def test_search_does_not_trust_unknown_category_reference(self):
        client = SearchClient({'security': {0: page([other_category(specializationId='missing')])}})
        with self.assertRaises(ParseError):
            self.collect(client)

    def test_invalid_search_settings_fail_before_network(self):
        for terms in ['security', None, [1], [' '], ['a'], ['b' * 61], ['security'] * 33]:
            with self.subTest(terms=terms):
                client = Mock()
                with self.assertRaises(ValueError):
                    self.collect(client, search_terms=terms)
                client.get.assert_not_called()


    def test_later_search_failure_preserves_old_snapshot_and_holds_queue(self):
        config = deepcopy(self.config)
        for name, settings in config['sources'].items():
            settings['enabled'] = name == 'sber'
        config['sources']['sber'] |= self.settings
        with tempfile.TemporaryDirectory() as directory:
            store = Store(str(Path(directory) / 'db'))
            telegram = Mock()
            try:
                previous = self.collect(SearchClient({'security': {0: page([other_category()])}}))
                store.save_snapshot('sber', [(v, decide(v, self.rules)) for v in previous])
                before = [dict(r) for r in store.db.execute('SELECT * FROM vacancies ORDER BY external_id')]
                failed = SearchClient({'security': {0: page([other_category()], total=2), 1: page([], total=2)}})
                result = scan(config, store, failed)
                self.assertTrue(result['sources']['sber']['error'])
                after = [dict(r) for r in store.db.execute('SELECT * FROM vacancies ORDER BY external_id')]
                self.assertEqual(before, after)
                Monitor(config, store, telegram, delivery_delay=0).dispatch_pending()
                telegram.send.assert_not_called()
                scan(config, store, SearchClient({'security': {0: page([other_category()])}}))
                Monitor(config, store, telegram, delivery_delay=0).dispatch_pending()
                self.assertEqual(telegram.send.call_count, 2)
                Monitor(config, store, telegram, delivery_delay=0).dispatch_pending()
                self.assertEqual(telegram.send.call_count, 2)
            finally:
                store.close()


if __name__ == '__main__':
    unittest.main()
