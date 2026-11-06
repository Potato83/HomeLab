import csv
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from botwork.career import application_text, deliver_reminders, execute_career, export_csv
from botwork.config import load_config
from botwork.croc_internships import parse_programmes
from botwork.http import FetchError
from botwork.jobs import handle_jobs, jobs_page, parse_search, search_jobs
from botwork.models import Decision, Vacancy
from botwork.pipeline import track_text, vacancy_ref
from botwork.runner import Monitor
from botwork.sources import ParseError
from botwork.storage import Store
from botwork.telegram import Telegram
from botwork.workflows import begin_input, handle_application_buttons, handle_input, handle_profile_buttons, profile_keyboard


class CareerToolsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(str(Path(self.tmp.name) / 'db'))
        self.addCleanup(self.store.close)
        self.config = load_config(str(Path(__file__).resolve().parents[1] / 'config.toml'))
        self.telegram = Telegram('dummy', '123')
        self.telegram.send = Mock()
        self.telegram.answer_callback = Mock()
        self.monitor = Monitor(self.config, self.store, self.telegram)
        self.addCleanup(self.monitor.close)
        self.v = Vacancy('mts', '1', 'Стажёр SOC', 'Компания', 'https://example.com', level='intern', work_format='remote')
        self.store.save_snapshot('mts', [(self.v, Decision(True, 'подходит'))])
        self.ref = vacancy_ref('mts', '1')

    def callback(self, data, seq=1, owner=123):
        return {'update_id': seq, 'callback_query': {'id': 'dummy', 'data': data, 'from': {'id': owner}, 'message': {'from': {'id': 123}, 'chat': {'id': 123, 'type': 'private'}}}}

    def message(self, text, seq=1, owner=123):
        return {'update_id': seq, 'message': {'from': {'id': owner}, 'chat': {'id': owner, 'type': 'private'}, 'text': text}}

    def test_notes_contact_next_and_replay(self):
        execute_career(self.store, '/note', self.ref + ' <b>заметка</b>', 1)
        execute_career(self.store, '/contact', self.ref + ' @recruiter', 2)
        execute_career(self.store, '/next', self.ref + ' Прочитать требования', 3)
        execute_career(self.store, '/note', self.ref + ' новое', 4)
        execute_career(self.store, '/note', self.ref + ' старое', 1)
        text = application_text(self.store, self.ref)
        self.assertIn('новое', text)
        self.assertNotIn('старое', text)
        self.assertIn('@recruiter', text)
        self.assertEqual(self.store.tracked_vacancies()[0]['status'], 'saved')
        execute_career(self.store, '/note', self.ref + ' -', 5)
        self.assertNotIn('новое', application_text(self.store, self.ref))

    def test_reminders_moscow_retry_delivery_and_deadline(self):
        execute_career(self.store, '/remind', self.ref + ' 2099-01-01 18:00 Ответ рекрутера', 1)
        reminder = self.store.db.execute('SELECT * FROM reminders').fetchone()
        self.assertIn('15:00:00+00:00', reminder['due_at'])
        self.store.db.execute("UPDATE reminders SET due_at='2000-01-01T00:00:00+00:00'")
        self.store.db.commit()
        self.telegram.send.side_effect = FetchError('offline')
        with self.assertRaises(FetchError):
            deliver_reminders(self.store, self.telegram)
        self.assertIsNone(self.store.db.execute('SELECT delivered_at FROM reminders').fetchone()[0])
        self.telegram.send.side_effect = None
        self.assertEqual(deliver_reminders(self.store, self.telegram), 1)
        self.assertEqual(deliver_reminders(self.store, self.telegram), 0)
        self.v.registration_deadline = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
        self.store.save_snapshot('mts', [(self.v, Decision(True, 'подходит'))])
        execute_career(self.store, '/remind', self.ref + ' deadline', 2)
        self.assertIn('дедлайн', self.store.db.execute('SELECT text FROM reminders').fetchone()[0])
        execute_career(self.store, '/remind', self.ref + ' off', 3)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM reminders').fetchone()[0], 0)

    def test_csv_unicode_and_formula_safety(self):
        self.v.title = '=HYPERLINK("bad")'
        self.store.save_snapshot('mts', [(self.v, Decision(True, 'подходит'))])
        execute_career(self.store, '/contact', self.ref + ' +formula', 1)
        raw = export_csv(self.store)
        rows = list(csv.reader(io.StringIO(raw.decode('utf-8-sig')), delimiter=';'))
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[1][2].startswith("'="))
        self.assertTrue(rows[1][6].startswith("'+"))

    def test_application_input_cancel_and_tracking_replay(self):
        self.assertFalse(handle_application_buttons(self.telegram, self.monitor, self.callback('app:note:' + self.ref, owner=999)))
        self.assertTrue(handle_application_buttons(self.telegram, self.monitor, self.callback('app:note:' + self.ref)))
        self.assertTrue(handle_input(self.telegram, self.monitor, self.message('Поговорить с HR', seq=2)))
        self.assertIn('Поговорить с HR', application_text(self.store, self.ref))
        begin_input(self.telegram, self.monitor, 'city')
        self.assertTrue(handle_input(self.telegram, self.monitor, self.message('/cancel', seq=3)))
        self.assertEqual(json.loads(self.store.get_state('input_request')), {})
        track_text(self.store, self.ref + ' applied', update_id=4)
        track_text(self.store, self.ref + ' interview', update_id=5)
        track_text(self.store, self.ref + ' applied', update_id=4)
        self.assertEqual(self.store.tracked_vacancies()[0]['status'], 'interview')

    def test_ignored_card_reminders_are_held_and_expired_input_rejected(self):
        execute_career(self.store, '/remind', self.ref + ' 2099-01-01 18:00 Ответ', 1)
        self.store.db.execute("UPDATE reminders SET due_at='2000-01-01T00:00:00+00:00'")
        self.store.db.commit()
        self.store.set_tracking(self.ref, 'ignored')
        self.assertEqual(deliver_reminders(self.store, self.telegram), 0)
        self.store.set_tracking(self.ref, 'saved')
        self.assertEqual(deliver_reminders(self.store, self.telegram), 1)
        begin_input(self.telegram, self.monitor, 'city')
        request = json.loads(self.store.get_state('input_request'))
        request['expires'] = 0
        self.store.set_state('input_request', json.dumps(request))
        handle_input(self.telegram, self.monitor, self.message('Москва', seq=2))
        self.assertEqual(json.loads(self.store.get_state('input_request')), {})

    def test_jobs_filters_pages_ignored_and_stale_navigation(self):
        records = [(Vacancy('mts', str(i), 'SOC стажёр ' + str(i), 'Компания', 'https://example.com/' + str(i), level='intern'), Decision(True, 'подходит')) for i in range(12)]
        self.store.save_snapshot('mts', records)
        options = parse_search('SOC source=mts direction=security page=2')
        self.assertEqual(len(search_jobs(self.store, self.config, options)), 12)
        text, markup = jobs_page(self.store, self.config, options, 'abcdef123456')
        self.assertIn('2/3', text)
        self.assertEqual(len(markup['inline_keyboard']), 6)
        self.store.set_tracking(vacancy_ref('mts', '2'), 'ignored')
        self.assertEqual(len(search_jobs(self.store, self.config, options)), 11)
        self.assertFalse(handle_jobs(self.telegram, self.monitor, self.message('/jobs', owner=999)))
        handle_jobs(self.telegram, self.monitor, self.message('/jobs SOC'))
        old = json.loads(self.store.get_state('jobs_view'))['token']
        handle_jobs(self.telegram, self.monitor, self.message('/jobs Компания', seq=2))
        handle_jobs(self.telegram, self.monitor, self.callback('jobs:' + old + ':2'))
        self.assertIn('устарел', self.telegram.answer_callback.call_args.args[1])

    def test_profile_creation_buttons_input_and_confirmation(self):
        button = profile_keyboard(self.monitor)['inline_keyboard'][0][0]['callback_data']
        self.assertFalse(handle_profile_buttons(self.telegram, self.monitor, self.callback(button, owner=999)))
        handle_profile_buttons(self.telegram, self.monitor, self.callback(button))
        self.assertFalse(handle_input(self.telegram, self.monitor, self.message('Мой профиль', seq=2, owner=999)))
        self.assertTrue(handle_input(self.telegram, self.monitor, self.message('Мой профиль', seq=2)))
        self.assertIn('Мой профиль', json.loads(self.store.get_state('search_profiles')))
        delete = profile_keyboard(self.monitor)['inline_keyboard'][0][1]['callback_data']
        handle_profile_buttons(self.telegram, self.monitor, self.callback(delete, seq=3))
        self.assertIn('Мой профиль', json.loads(self.store.get_state('search_profiles')))
        confirm = self.telegram.send.call_args.kwargs['reply_markup']['inline_keyboard'][0][0]['callback_data']
        handle_profile_buttons(self.telegram, self.monitor, self.callback(confirm, seq=4))
        self.assertEqual(json.loads(self.store.get_state('search_profiles')), {})

    def test_croc_programme_is_always_unverified_and_incomplete_rejected(self):
        html = '<h1>Направления стажировки Кибербезопасность Программная и вычислительная инфраструктура</h1>'
        for department in ['cybersecurity-ru', 'system-integration-and-infrastructure-solutions-ru']:
            html += '<a href="https://www.croc.ru/career/job-search/?_sft_departament=' + department + '&amp;_sft_expirience=no-work-experience-ru">Вакансии</a>'
        rows = parse_programmes(html)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row.recruitment_status == 'unknown' for row in rows))
        with self.assertRaises(ParseError):
            parse_programmes(html.replace('cybersecurity-ru', 'different'))

    def test_backup_roundtrip_preserves_new_application_data(self):
        from botwork.maintenance import backup_database
        execute_career(self.store, '/note', self.ref + ' Сохранить заметку', 1)
        execute_career(self.store, '/remind', self.ref + ' 2099-01-01 18:00 Ответ', 2)
        backup = str(Path(self.tmp.name) / 'backup.db')
        backup_database(str(Path(self.tmp.name) / 'db'), backup)
        restored = Store(backup)
        try:
            self.assertIn('Сохранить заметку', application_text(restored, self.ref))
            self.assertEqual(restored.db.execute('SELECT COUNT(*) FROM reminders').fetchone()[0], 1)
            self.assertEqual(restored.db.execute('SELECT COUNT(*) FROM career_commands').fetchone()[0], 2)
        finally:
            restored.close()
