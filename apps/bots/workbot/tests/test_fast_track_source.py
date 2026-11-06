from copy import deepcopy
from dataclasses import asdict, replace
import json
from pathlib import Path
import ssl
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import URLError

from botwork.config import load_config
from botwork.fast_track_source import collect_fast_track, fast_format, parse_fast_form, parse_fast_track
from botwork.http import FetchError, HttpClient
from botwork.matching import decide
from botwork.reporting import source_views, sources_text, status_text
from botwork.runner import Monitor
from botwork.service import scan
from botwork.sources import ParseError
from botwork.storage import Store
from botwork.telegram import format_message

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

    def test_only_explicit_ib_directions_with_own_formats(self):
        rows = self.parse()
        self.assertEqual(len(rows), 2)
        self.assertEqual((rows[0].work_format, rows[0].cities), ('remote', ['Москва', 'Санкт-Петербург']))
        self.assertEqual((rows[1].work_format, rows[1].cities), ('hybrid', ['Москва']))
        self.assertTrue(decide(rows[0], self.rules, True).matched)
        self.assertFalse(decide(rows[1], self.rules, True).matched)
        self.assertEqual(rows[0].application_url, FORM_URL)
        self.assertTrue(rows[0].url.endswith('#heading-1'))

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

    def test_button_is_not_intake_confirmation_and_opt_in_required(self):
        row = self.parse()[0]
        self.assertEqual(row.recruitment_status, 'unknown')
        self.assertFalse(decide(row, self.rules).matched)
        allowed = decide(row, self.rules, True)
        self.assertTrue(allowed.matched)
        self.assertIn('Набор не подтверждён', allowed.warnings[0])

    def test_status_warning_does_not_count_as_missing_location(self):
        row = replace(self.parse()[0], work_format='hybrid', cities=['Санкт-Петербург'])
        rules = self.rules | {'include_unknown_location': False}
        self.assertTrue(decide(row, rules, True).matched)
        self.assertFalse(decide(replace(row, cities=[]), rules, True).matched)

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

    def test_unknown_notification_clear_escaped_and_linked(self):
        row = replace(self.parse()[0], notes=['<unknown>'])
        message = format_message({'vacancy': asdict(row), 'decision': asdict(decide(row, self.rules, True))})
        self.assertIn('<b>Набор не подтверждён</b>', message)
        self.assertIn('🟡', message)
        self.assertIn(FORM_URL, message)
        self.assertIn('&lt;unknown&gt;', message)

    def test_tls_failure_has_specific_safe_reason(self):
        with patch('botwork.http.build_opener') as build:
            build.return_value.open.side_effect = URLError(ssl.SSLCertVerificationError('secret URL'))
            with self.assertRaisesRegex(FetchError, '^Ошибка проверки TLS-сертификата$'):
                HttpClient(delay=0)._get_once(FORM_URL)

    def test_scan_status_unknown_is_ready_and_can_be_delivered(self):
        client = Mock()
        client.get.side_effect = [document(), FetchError('TLS')]
        with tempfile.TemporaryDirectory() as directory:
            store = Store(str(Path(directory) / 'db'))
            try:
                result = scan(self.config, store, client)
                self.assertEqual(result['sources']['pt_fast_track'], {'found': 2, 'matched': 1, 'error': None, 'unverified': 2})
                view = next(row for row in source_views(store, self.config) if row['source'] == 'pt_fast_track')
                self.assertEqual((view['state'], view['unverified'], view['failure_count']), ('ready', 2, 0))
                self.assertIn('Набор не подтверждён', status_text(store, self.config))
                self.assertIn('неподтверждённый', sources_text(store, self.config))
                telegram = Mock()
                Monitor(self.config, store, telegram, delivery_delay=0).dispatch_pending()
                self.assertEqual(telegram.send.call_count, 1)
            finally:
                store.close()

    def test_default_opt_out_filters_unverified(self):
        self.config['sources']['pt_fast_track'].pop('notify_unverified')
        client = Mock()
        client.get.side_effect = [document(), FetchError('TLS')]
        result = scan(self.config, client=client)
        self.assertEqual(result['sources']['pt_fast_track']['matched'], 0)

    def test_invalid_status_does_not_match(self):
        self.assertFalse(decide(replace(self.parse()[0], recruitment_status='broken'), self.rules, True).matched)


class IntakeDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / 'bot.sqlite3')
        self.store = Store(self.path)
        self.rules = load_config(str(ROOT / 'config.toml'))['filters']
        self.row = parse_fast_track(document(), URL, self.rules)[0]
        self.source = self.row.source

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def snapshot(self, status):
        row = replace(self.row, active=status != 'closed', recruitment_status=status)
        self.store.save_snapshot(self.source, [(row, decide(row, self.rules, True))])
        return self.store.pending([self.source], 20)

    def send(self, pending):
        self.assertEqual(len(pending), 1)
        message = format_message(json.loads(pending[0]['payload']))
        self.store.mark_sent(pending[0])
        return message

    def test_unknown_once_restart_confirmation_once_and_no_flap_spam(self):
        self.assertIn('Набор не подтверждён', self.send(self.snapshot('unknown')))
        self.assertFalse(self.snapshot('unknown'))
        self.store.close()
        self.store = Store(self.path)
        self.assertFalse(self.snapshot('unknown'))
        self.assertIn('Набор теперь подтверждён', self.send(self.snapshot('open')))
        self.assertFalse(self.snapshot('open'))
        self.assertFalse(self.snapshot('unknown'))
        self.assertFalse(self.snapshot('open'))

    def test_failed_confirmation_held_on_outage_retries_same_generation(self):
        self.send(self.snapshot('unknown'))
        initial = self.snapshot('open')
        self.assertEqual(initial[0]['generation'], 2)
        self.assertFalse(self.snapshot('unknown'))
        retry = self.snapshot('open')
        self.assertEqual(retry[0]['generation'], 2)
        self.assertIn('Набор теперь подтверждён', self.send(retry))
        self.assertFalse(self.snapshot('open'))

    def test_unsent_open_then_unknown_first_delivery_then_confirmation(self):
        self.assertEqual(len(self.snapshot('open')), 1)
        self.assertIn('Набор не подтверждён', self.send(self.snapshot('unknown')))
        self.assertIn('Набор теперь подтверждён', self.send(self.snapshot('open')))

    def test_close_reopen_is_new_episode(self):
        self.send(self.snapshot('unknown'))
        self.send(self.snapshot('open'))
        self.assertFalse(self.snapshot('closed'))
        self.assertIn('Набор не подтверждён', self.send(self.snapshot('unknown')))
        self.assertIn('Набор теперь подтверждён', self.send(self.snapshot('open')))

    def test_disappearance_reappearance_is_new_episode(self):
        self.send(self.snapshot('unknown'))
        self.store.save_snapshot(self.source, [])
        self.assertEqual(len(self.snapshot('unknown')), 1)

    def test_legacy_sent_payload_is_not_repeated(self):
        pending = self.snapshot('open')
        self.send(pending)
        payload = json.loads(pending[0]['payload'])
        payload.pop('_notified_status')
        payload.pop('_notification_kind')
        payload['vacancy'].pop('recruitment_status')
        payload['vacancy'].pop('application_url')
        with self.store.db:
            self.store.db.execute('UPDATE vacancies SET payload=?', (json.dumps(payload),))
        self.assertFalse(self.snapshot('open'))
        self.assertFalse(self.snapshot('unknown'))
        self.assertFalse(self.snapshot('open'))
        self.assertIn('Стажировка', format_message(payload))


if __name__ == '__main__':
    unittest.main()
