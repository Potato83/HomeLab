from copy import deepcopy
from dataclasses import asdict, replace
import json
from pathlib import Path
import ssl
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import URLError

from workbot.config import load_config
from workbot.fast_track_source import collect_fast_track, fast_format, parse_fast_form, parse_fast_track
from workbot.http import FetchError, HttpClient
from workbot.matching import decide
from workbot.runner import Monitor
from workbot.service import scan
from workbot.sources import ParseError
from workbot.storage import Store
from workbot.telegram import format_message

ROOT = Path(__file__).resolve().parents[1]
URL = 'https://pt-start.ru/fast-track'
FORM_URL = 'https://form-pt-start.ptsecurity.com/internships/2026'


def direction(number, title, work_format):
    return f'''<div class="t-rec" id="heading-{number}"><div class="tn-group content{number}">
    <div class="tn-atom">{title}</div></div></div>
    <div class="t-rec"><div class="tn-atom">Нам важны</div>
    <div class="tn-atom">Интерес к информационной безопасности</div>
    <div class="tn-atom">Формат работы</div><div class="tn-atom">{work_format}</div></div>'''


def document(status='', application=None):
    application = status if application is None else application
    return f'''<div class="t-rec" id="hero"><div class="tn-atom">Стажировка Fast Track</div>
    <div class="tn-atom">{status}</div><a href="{FORM_URL}">Подать заявку</a></div>
    <div class="t-rec"><a name="steps"></a></div>
    <div class="t-rec"><div class="tn-atom">Получи офер на стажировку</div></div>
    <div class="t-rec"><a name="speciality"></a></div>
    <div class="t-rec"><div class="tn-group content10"><div class="tn-atom">Разработка Golang</div></div></div>
    {direction(1, 'Кибербезопасность Продуктовая экспертиза (PT Sandbox)', 'Удаленка. Гибрид (Москва, Санкт-Петербург и другие города).')}
    {direction(2, 'Кибербезопасность Управление проектами', 'Гибрид (Москва) с возможностью выезда к заказчикам. От 30 часов в неделю.')}
    {direction(3, 'Тестирование QA', 'Удаленка')}
    <div class="t-rec" id="application"><div class="tn-atom">Сделай первый шаг</div>
    <div class="tn-atom">{application}</div><a class="tn-atom" href="{FORM_URL}">Подать заявку</a></div>
    <div class="t-rec"><div class="tn-atom">Прошлогодний набор открыт</div></div>'''


def form(button='Отправить заявку', attributes='', heading='Стажировка Fast Track'):
    return f'''<main><h1>{heading}</h1><form><fieldset {attributes}>
    <input name="name"><input type="email" name="email"><button type="submit">{button}</button>
    </fieldset></form></main>'''


class FastTrackTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config(str(ROOT / 'config.toml'))
        self.rules = self.config['filters']
        for name, settings in self.config['sources'].items():
            settings['enabled'] = name == 'pt_fast_track'
        self.settings = self.config['sources']['pt_fast_track']

    def parse(self, html=None):
        return parse_fast_track(document() if html is None else html, URL, self.rules)


    def test_missing_direction_body_does_not_borrow_neighbor_description(self):
        heading = '<div class="t-rec" id="heading-missing"><div class="tn-group content99"><div class="tn-atom">Кибербезопасность Без описания</div></div></div>'
        html = document().replace(direction(1, 'Кибербезопасность Продуктовая экспертиза (PT Sandbox)', 'Удаленка. Гибрид (Москва, Санкт-Петербург и другие города).'), heading)
        rows = self.parse(html)
        self.assertEqual(len(rows), 1)
        self.assertIn('Управление проектами', rows[0].title)
        self.assertIn('Без описания', rows[0].notes[0])
        client = Mock()
        client.get.side_effect = [html, form()]
        fetched = collect_fast_track(self.settings, client, self.rules)
        self.assertTrue(any('Без описания' in note for note in fetched[0].notes))



    def test_closed_and_conflicting_page_status(self):
        rows = self.parse(document('Набор заявок завершен'))
        self.assertTrue(all(not row.active and row.recruitment_status == 'closed' for row in rows))
        self.assertFalse(decide(rows[0], self.rules, True).matched)
        with self.assertRaises(ParseError):
            self.parse(document('Набор заявок открыт', 'Набор заявок завершен'))

    def test_format_independent_remote_option_vs_hybrid_days(self):
        for label, expected in [('Удаленка. Гибрид (Москва)', 'remote'),
                                ('Удалённо или гибрид (СПб)', 'remote'),
                                ('Гибрид: два дня удалённо', 'hybrid'),
                                ('Гибрид (Москва), удалёнка не предусмотрена', 'hybrid'),
                                ('Офис (Санкт-Петербург)', 'office'), ('Уточняется', 'unknown')]:
            with self.subTest(label=label):
                self.assertEqual(fast_format(label)[0], expected)

    def test_structure_loss_fails_instead_of_empty_snapshot(self):
        for old, new in [('name="steps"', ''), ('Стажировка Fast Track', 'Курс Fast Track'),
                         ('Получи офер на стажировку', 'Сертификат'), ('Сделай первый шаг', 'Архив'),
                         ('Нам важны', 'Посторонний текст'), ('id="heading-1"', '')]:
            with self.subTest(old=old), self.assertRaises(ParseError):
                self.parse(document().replace(old, new, 1))

    def test_untrusted_or_ambiguous_form_link_fails(self):
        for url in ['http://form-pt-start.ptsecurity.com/internships/2026',
                    'https://example.com/internships/2026', 'https://form-pt-start.ptsecurity.com/newsletter']:
            with self.subTest(url=url), self.assertRaises(ParseError):
                self.parse(document().replace(FORM_URL, url))
        with self.assertRaises(ParseError):
            self.parse(document().replace(FORM_URL, FORM_URL + '/', 1))

    def test_duplicate_conflict_fails(self):
        extra = direction(4, 'Кибербезопасность Управление проектами', 'Офис (Санкт-Петербург)')
        with self.assertRaises(ParseError):
            self.parse(document().replace('<div class="t-rec" id="application">', extra + '<div class="t-rec" id="application">'))

    def test_form_open_requires_usable_application_not_newsletter(self):
        self.assertIs(parse_fast_form(form())[0], True)
        for html in [form(button='Подписаться'), form(attributes='disabled'),
                     form(heading='Курс Fast Track'), form(heading='Стажировка другой компании'),
                     '<main><h1>Стажировка Fast Track</h1><div id="app"></div></main>',
                     form().replace('type="email"', 'type="hidden"')]:
            with self.subTest(html=html):
                self.assertIsNone(parse_fast_form(html)[0])

    def test_form_closed_and_conflict(self):
        self.assertIs(parse_fast_form('<main><h1>Стажировка Fast Track: набор заявок завершен</h1></main>')[0], False)
        with self.assertRaises(ParseError):
            parse_fast_form('<main><h1>Стажировка Fast Track: набор открыт, регистрация закрыта</h1></main>')

    def test_form_network_failure_keeps_unverified_rows(self):
        client = Mock()
        client.get.side_effect = [document(), FetchError('Ошибка проверки TLS-сертификата')]
        rows = collect_fast_track(self.settings, client, self.rules)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(v.recruitment_status == 'unknown' for v in rows))
        self.assertIn('TLS-сертификата', rows[0].notes[0])
        self.assertEqual(client.get.call_args.args, (FORM_URL,))

    def test_closed_page_skips_form_and_form_can_close_or_confirm(self):
        client = Mock()
        client.get.return_value = document('Набор заявок завершен')
        self.assertFalse(collect_fast_track(self.settings, client, self.rules)[0].active)
        self.assertEqual(client.get.call_count, 1)
        for html, expected in [(form(), 'open'), ('<h1>Стажировка Fast Track: набор заявок завершен</h1>', 'closed')]:
            client.get.side_effect = [document(), html]
            self.assertEqual(collect_fast_track(self.settings, client, self.rules)[0].recruitment_status, expected)
        client.get.side_effect = [document('Набор заявок открыт'), '<h1>Стажировка Fast Track: набор завершен</h1>']
        with self.assertRaises(ParseError):
            collect_fast_track(self.settings, client, self.rules)


    def test_tls_failure_has_specific_safe_reason(self):
        with patch('workbot.http.build_opener') as build:
            build.return_value.open.side_effect = URLError(ssl.SSLCertVerificationError('secret URL'))
            with self.assertRaisesRegex(FetchError, '^Ошибка проверки TLS-сертификата$'):
                HttpClient(delay=0)._get_once(FORM_URL)



    def test_invalid_status_does_not_match(self):
        self.assertFalse(decide(replace(self.parse()[0], recruitment_status='broken'), self.rules, True).matched)




if __name__ == '__main__':
    unittest.main()
