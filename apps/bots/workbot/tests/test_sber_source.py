from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from workbot.config import load_config
from workbot.matching import decide
from workbot.runner import Monitor
from workbot.sber_source import (collect_sber, parse_dictionary, parse_publications,
    public_slug, salary_text, work_conditions)
from workbot.service import scan as real_scan

def scan(config, *args, **kwargs):
    with patch('workbot.service.source_settings', side_effect=lambda n, s, r: s), patch('workbot.service.collection_rules', side_effect=lambda r: r):
        return real_scan(config, *args, **kwargs)

from workbot.sources import ParseError
from workbot.storage import Store

ROOT = Path(__file__).resolve().parents[1]
CATEGORY = 'Информационные технологии:Специалист по информационной безопасности'
CATEGORIES = {'ib': CATEGORY}
EXPERIENCES = {'none': 'Без опыта', 'one': 'От 1 года до 3 лет', 'three': 'От 3 до 6 лет'}
SCHEDULES = {'1': 'Полный день', '4': 'Удаленная работа'}


def dictionary(items):
    return json.dumps({'success': True, 'data': [dict(id=key, value=value, isActive=True) for key, value in items.items()]})


def vacancy(identifier=123, **changes):
    return dict(internalId=identifier, title='Стажер SOC', company='Сбер',
        specialization=CATEGORY, specializationId='ib', experienceId='none',
        workScheduleId=1, city='г Санкт-Петербург', region='',
        introduction='Мониторинг событий', duties='Расследование инцидентов',
        requirements='Основы сетей', conditions='Гибридный формат работы',
        salary_min=None, salary_max=None) | changes


def page(rows=None, total=None):
    rows = [vacancy()] if rows is None else rows
    return json.dumps({'success': True, 'data': {'total': len(rows) if total is None else total, 'vacancies': rows}})


class Client:
    def __init__(self, pages=None):
        self.calls = []
        self.pages = pages or {0: page()}

    def get(self, url):
        self.calls.append(url)
        path = urlsplit(url).path
        if path.endswith('/profAreas'):
            return dictionary(CATEGORIES | {'guard': 'Безопасность:Охранник'})
        if path.endswith('/experiences'):
            return dictionary(EXPERIENCES)
        if path.endswith('/schedules'):
            return dictionary(SCHEDULES)
        query = parse_qs(urlsplit(url).query)
        if 'ib' not in query.get('profAreas', []):
            raise AssertionError('Wrong category query')
        return self.pages[int(query['skip'][0])]


class SberSourceTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config(str(ROOT / 'config.toml'))
        self.config['filters']['directions'] = ['security']  # Legacy security snapshot fixtures.
        self.rules = self.config['filters']
        self.rules['security_patterns'] = [r'информационн.*безопасност']
        self.settings = self.config['sources']['sber'] | {'page_size': 1, 'search_terms': []}

    def parse(self, rows=None, total=None, skip=0, size=1):
        return parse_publications(page(rows, total), 'https://rabota.sber.ru', skip, size,
            CATEGORIES, EXPERIENCES, SCHEDULES)

    def test_spb_hybrid_intern_and_official_link(self):
        row = self.parse()[0][0]
        self.assertEqual(row.level, 'intern')
        self.assertEqual(row.work_format, 'hybrid')
        self.assertEqual(row.url, 'https://rabota.sber.ru/search/stazher-soc-123/')
        self.assertTrue(decide(row, self.rules).matched)

    def test_experience_dictionary_and_remote_any_city(self):
        row = self.parse([vacancy(title='Специалист SOC', city='г Казань', workScheduleId=4, conditions='')])[0][0]
        self.assertEqual(row.level, 'junior')
        self.assertEqual(row.work_format, 'remote')
        self.assertTrue(decide(row, self.rules).matched)

    def test_full_day_is_not_assumed_office(self):
        row = self.parse([vacancy(city='Москва', conditions='ДМС и обучение')])[0][0]
        self.assertEqual(row.work_format, 'unknown')
        decision = decide(row, self.rules)
        self.assertTrue(decision.matched)

    def test_moscow_office_and_partial_remote_hybrid_rejected(self):
        for conditions in ['Современный офис в д. Аносино, ул. Университетская, вл. 11',
                           'Гибридный формат работы\n90 дней в году удаленной работы',
                           'Работа из офиса, удалённая работа не предусмотрена']:
            with self.subTest(conditions=conditions):
                row = self.parse([vacancy(city='г Москва', conditions=conditions)])[0][0]
                self.assertIn(row.work_format, ('office', 'hybrid'))
                self.assertTrue(decide(row, self.rules).matched)

    def test_remote_schedule_with_office_amenity_stays_remote(self):
        self.assertEqual(work_conditions('Удаленная работа', 'Современный офис в Москве')[0], 'remote')
        self.assertEqual(work_conditions('Удаленная работа', 'Работа в офисе в Москве')[0], 'office')

    def test_requirements_remote_access_is_not_work_format(self):
        row = self.parse([vacancy(conditions='', requirements='Знание удаленного доступа и гибридных облаков')])[0][0]
        self.assertEqual(row.work_format, 'unknown')

    def test_experience_from_three_overrides_intern_title(self):
        row = self.parse([vacancy(experienceId='three')])[0][0]
        self.assertEqual(row.level, 'experienced')
        self.assertTrue(decide(row, self.rules).matched)

    def test_one_to_three_not_misread_as_three_year_minimum(self):
        row = self.parse([vacancy(title='Junior SOC', experienceId='one')])[0][0]
        self.assertEqual(row.level, 'unknown')
        self.assertTrue(decide(row, self.rules).matched)
        row.title = 'Специалист SOC'
        self.assertTrue(decide(row, self.rules).matched)

    def test_missing_conditions_and_location_warn_without_guessing(self):
        row = self.parse([vacancy(workScheduleId=None, experienceId=None, city=None, region=None, conditions=None)])[0][0]
        decision = decide(row, self.rules)
        self.assertEqual(row.cities, [])
        self.assertEqual(row.work_format, 'unknown')
        self.assertTrue(decision.matched)

    def test_region_fallback(self):
        row = self.parse([vacancy(city=None, region='г Санкт-Петербург')])[0][0]
        self.assertEqual(row.cities, ['г Санкт-Петербург'])

    def test_unknown_references_and_ignored_category_filter_are_errors(self):
        for changes in [dict(specializationId='guard'),
                        dict(workScheduleId=999), dict(experienceId='new'), dict(specializationId=None)]:
            with self.subTest(changes=changes), self.assertRaises(ParseError):
                self.parse([vacancy(**changes)])

    def test_missing_or_invalid_card_fields_fail(self):
        for changes in [dict(internalId=True), dict(internalId=-1), dict(title=''), dict(company=None),
                        dict(city=[]), dict(requirements={}), dict(duties=None, requirements=None, introduction=None)]:
            with self.subTest(changes=changes), self.assertRaises(ParseError):
                self.parse([vacancy(**changes)])

    def test_dictionary_failures_not_treated_as_empty_snapshot(self):
        texts = ['{}', '<html>error</html>', '{"success":false,"data":[]}', dictionary({}),
                 dictionary(CATEGORIES).replace('true', '"true"'),
                 json.dumps({'success': True, 'data': [{'id': [], 'value': CATEGORY, 'isActive': True}]}),
                 json.dumps({'success': True, 'data': [{'id': 1, 'value': CATEGORY, 'isActive': True}] * 2})]
        for text in texts:
            with self.subTest(text=text[:100]), self.assertRaises(ParseError):
                parse_dictionary(text)

    def test_incomplete_and_unsuccessful_pages_fail(self):
        for text in [page([], total=1), page(total=-1), page(total=True),
                     page().replace('"success": true', '"success": false'), '<html>error</html>', '{}']:
            with self.subTest(text=text[:80]), self.assertRaises(ParseError):
                parse_publications(text, 'https://rabota.sber.ru', 0, 1, CATEGORIES, EXPERIENCES, SCHEDULES)

    def test_explicit_zero_is_success(self):
        self.assertEqual(self.parse([], total=0), ([], 0))

    def test_duplicate_ids_inside_page_fail(self):
        with self.assertRaises(ParseError):
            self.parse([vacancy(), vacancy()], size=2)

    def test_all_pages_collected_with_repeated_category_parameters(self):
        client = Client({0: page([vacancy(1)], total=2), 1: page([vacancy(2)], total=2)})
        rows = collect_sber(self.settings, client, self.rules)
        self.assertEqual([row.external_id for row in rows], ['1', '2'])
        self.assertEqual(len(client.calls), 5)
        self.assertTrue(all(row.source == 'sber' for row in rows))

    def test_duplicate_pages_or_changing_count_fail(self):
        for second in [page([vacancy(1)], total=2), page([vacancy(2)], total=3)]:
            with self.subTest(second=second), self.assertRaises(ParseError):
                collect_sber(self.settings, Client({0: page([vacancy(1)], total=2), 1: second}), self.rules)

    def test_page_limit_fails_before_partial_snapshot(self):
        with self.assertRaisesRegex(ParseError, 'max_pages'):
            collect_sber(self.settings | {'max_pages': 1}, Client({0: page(total=2)}), self.rules)

    def test_lost_ib_category_is_error(self):
        client = Mock()
        client.get.return_value = dictionary({'guard': 'Безопасность:Охранник'})
        with self.assertRaisesRegex(ParseError, 'категории ИБ'):
            collect_sber(self.settings, client, self.rules)

    def test_salary_does_not_invent_currency(self):
        self.assertEqual(salary_text(vacancy(salary_min=60000, salary_max=90000)), 'от 60 000 до 90 000 (валюта не указана)')
        for changes in [dict(salary_min=-1), dict(salary_min='60000'), dict(salary_max=float('inf')),
                        dict(salary_min=90000, salary_max=60000)]:
            with self.subTest(changes=changes), self.assertRaises(ParseError):
                salary_text(vacancy(**changes))

    def test_slug_agrees_with_observed_public_url(self):
        self.assertEqual(public_slug('Стажер (Администрирование СЗИ)'), 'stazher-administrirovanie-szi')
        self.assertEqual(public_slug('C#-разработчик'), 'c-razrabotchik')

    def test_failed_later_page_preserves_database_and_holds_delivery(self):
        config = deepcopy(self.config)
        for name, settings in config['sources'].items():
            settings['enabled'] = name == 'sber'
        config['sources']['sber']['page_size'] = 1
        config['sources']['sber']['search_terms'] = []
        row = self.parse()[0][0]
        with tempfile.TemporaryDirectory() as directory:
            store = Store(str(Path(directory) / 'db'))
            try:
                store.save_snapshot('sber', [(row, decide(row, self.rules))])
                client = Client({0: page([vacancy(1)], total=2), 1: page([], total=2)})
                result = scan(config, store, client)
                self.assertTrue(result['sources']['sber']['error'])
                self.assertEqual(store.pending(['sber'], 20), [])
                telegram = Mock()
                Monitor(config, store, telegram, delivery_delay=0).dispatch_pending()
                telegram.send.assert_not_called()
                # A successful complete snapshot releases the original item once.
                scan(config, store, Client())
                Monitor(config, store, telegram, delivery_delay=0).dispatch_pending()
                self.assertEqual(telegram.send.call_count, 1)
            finally:
                store.close()

    def test_sber_config_validation(self):
        original = (ROOT / 'config.toml').read_text()
        for old, new in [('page_size = 100\nmax_pages = 50', 'page_size = 0\nmax_pages = 50'),
                         ('page_size = 100\nmax_pages = 50', 'page_size = 100\nmax_pages = 0'),
                         ('https://rabota.sber.ru/public/', 'https://example.com/public/')]:
            with self.subTest(new=new), tempfile.TemporaryDirectory() as directory:
                file = Path(directory) / 'config.toml'
                file.write_text(original.replace(old, new))
                with self.assertRaises(ValueError):
                    load_config(str(file))


if __name__ == '__main__':
    unittest.main()
