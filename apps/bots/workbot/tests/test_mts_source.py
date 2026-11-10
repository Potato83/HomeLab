from copy import deepcopy
import json
from pathlib import Path
import unittest
from urllib.parse import parse_qs, urlsplit

from workbot.config import load_config
from workbot.matching import decide
from workbot.mts_source import collect_mts, parse_mts_detail, parse_mts_page, salary_text
from workbot.sources import ParseError

ROOT = Path(__file__).resolve().parents[1]


def row(identifier="720206481072327882", **changes):
    return dict(id=720206481072327900, slug=identifier, title="Стажёр по кибербезопасности",
        isActive=True, region={"title": "Москва"}, experience={"title": "Без опыта"},
        organization={"title": "МТС"}, workFormats=[{"title": "Удаленная работа"}],
        workTypes=[{"title": "Стажировка"}], categories=[{"title": "Работа в IT"}],
        salaryFrom=None, salaryTo=None, description=None, currency={"title": "RUB"}) | changes


def page(rows=None, number=1, size=1, total=1):
    return json.dumps({"data": [row()] if rows is None else rows, "meta": {"pagination": {
        "page": number, "pageSize": size, "pageCount": (total + size - 1) // size,
        "total": total}}}, ensure_ascii=False)


class MtsTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config(str(ROOT / 'config.toml'))
        self.rules = self.config['filters']
        self.settings = self.config['sources']['mts'] | {'page_size': 1, 'search_terms': ['soc']}

    def test_exact_string_id_and_remote_match(self):
        v = parse_mts_page(page(), 'https://job.mts.ru', 1, 1)[0][0]
        self.assertEqual(v.external_id, '720206481072327882')
        self.assertTrue(v.url.endswith('/720206481072327882'))
        self.assertTrue(decide(v, self.rules).matched)
        self.assertEqual(v.work_format, 'remote')

    def test_rounded_numeric_id_is_not_fallback(self):
        with self.assertRaises(ParseError):
            parse_mts_page(page([row(slug=None)]), 'https://job.mts.ru', 1, 1)

    def test_inactive_not_notified(self):
        v = parse_mts_page(page([row(isActive=False)]), 'https://job.mts.ru', 1, 1)[0][0]
        self.assertFalse(decide(v, self.rules).matched)

    def test_invalid_status_not_assumed_active(self):
        with self.assertRaises(ParseError):
            parse_mts_page(page([row(isActive='false')]), 'https://job.mts.ru', 1, 1)

    def test_moscow_office_not_remote(self):
        v = parse_mts_page(page([row(workFormats=[{'title': 'В офисе'}])]), 'https://job.mts.ru', 1, 1)[0][0]
        self.assertTrue(decide(v, self.rules).matched)

    def test_remote_option_with_hybrid_is_available(self):
        v = parse_mts_page(page([row(workFormats=[{'title': 'Гибридный'}, {'title': 'Удаленная работа'}])]), 'https://job.mts.ru', 1, 1)[0][0]
        self.assertEqual(v.work_format, 'remote')

    def test_experience_wins_over_intern_label(self):
        v = parse_mts_page(page([row(experience={'title': '3-6 лет'})]), 'https://job.mts.ru', 1, 1)[0][0]
        self.assertTrue(decide(v, self.rules).matched)

    def test_unknown_metadata_stays_unknown(self):
        v = parse_mts_page(page([row(region=None, workFormats=None)]), 'https://job.mts.ru', 1, 1)[0][0]
        d = decide(v, self.rules)
        self.assertTrue(d.matched)

    def test_empty_catalogue(self):
        rows, meta = parse_mts_page(page([], total=0), 'https://job.mts.ru', 1, 1)
        self.assertEqual(rows, [])
        self.assertEqual(meta['total'], 0)

    def test_invalid_json_and_incomplete_page(self):
        for text in ('<html>error</html>', '{}', page([], total=1), page([row()], number=2)):
            with self.subTest(text=text), self.assertRaises(ParseError):
                parse_mts_page(text, 'https://job.mts.ru', 1, 1)

    def test_duplicate_ids_within_page(self):
        with self.assertRaises(ParseError):
            parse_mts_page(page([row(), row()], size=2, total=2), 'https://job.mts.ru', 1, 2)

    def test_salary_extraction(self):
        self.assertEqual(salary_text(row(salaryFrom=60000, salaryTo=60000)), '60 000 ₽')
        self.assertEqual(salary_text(row(salaryFrom=None, salaryTo=90000)), 'до 90 000 ₽')
        self.assertEqual(salary_text(row(salaryFrom=60000, salaryTo=90000)), 'от 60 000 до 90 000 ₽')
        with self.assertRaises(ParseError):
            salary_text(row(salaryFrom=-1))

    def test_detail_scope_and_existing_metadata(self):
        v = parse_mts_page(page(), 'https://job.mts.ru', 1, 1)[0][0]
        html = '''<h2 class="vacancy__header__title">Стажёр по кибербезопасности</h2>
        <section class="vacancy__content"><div class="vacancy__data">Мониторинг событий ИБ</div></section>
        <div>Другие вакансии: гибрид, старший инженер</div>
        <dialog>Вакансия в архиве</dialog>'''
        result = parse_mts_detail(html, v)
        self.assertNotIn('Другие', result.description)
        self.assertTrue(result.active)  # Modal text does not override structured API status.
        self.assertEqual(result.work_format, 'remote')

    def test_collect_all_pages(self):
        class Client:
            def get(self, url):
                if not urlsplit(url).query:
                    return '<h2 class="vacancy__header__title">Senior SOC</h2><section class="vacancy__content"><div class="vacancy__data">SIEM</div></section>'
                number = int(parse_qs(urlsplit(url).query)['pagination[page]'][0])
                return page([row(str(720206481072327880 + number), title='Senior SOC')], number, total=2)
        result = collect_mts(self.settings, Client(), self.rules)
        self.assertEqual(len(result), 2)

    def test_repeat_page_and_catalog_change_fail(self):
        for mode in ['duplicate', 'changed']:
            with self.subTest(mode=mode):
                class Client:
                    def get(self, url):
                        if not urlsplit(url).query:
                            return '<h2 class="vacancy__header__title">Senior SOC</h2><section class="vacancy__content"><div class="vacancy__data">SIEM</div></section>'
                        number = int(parse_qs(urlsplit(url).query)['pagination[page]'][0])
                        identifier = '720206481072327882' if mode == 'duplicate' else str(720206481072327880 + number)
                        return page([row(identifier, title='Senior SOC')], number, total=3 if mode == 'changed' and number == 2 else 2)
                with self.assertRaises(ParseError):
                    collect_mts(self.settings, Client(), self.rules)

    def test_page_cap(self):
        class Client:
            def get(self, url):
                return page([row(title='Senior SOC')], total=2)
        with self.assertRaises(ParseError):
            collect_mts(self.settings | {'max_pages': 1}, Client(), self.rules)

    def test_extra_api_results_are_filtered_locally(self):
        class Client:
            def get(self, url):
                return page([row(title='Продавец')])
        rows = collect_mts(self.settings, Client(), self.rules)
        self.assertEqual(len(rows), 1)
        self.assertFalse(decide(rows[0], self.rules).matched)

    def test_overlapping_search_results_deduplicated(self):
        class Client:
            def get(self, url):
                if not urlsplit(url).query:
                    return '<h2 class="vacancy__header__title">Senior SOC Security</h2><section class="vacancy__content"><div class="vacancy__data">SIEM</div></section>'
                return page([row(title='Senior SOC Security')])
        rows = collect_mts(self.settings | {'search_terms': ['soc', 'security']}, Client(), self.rules)
        self.assertEqual(len(rows), 1)


if __name__ == '__main__':
    unittest.main()
