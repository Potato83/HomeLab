from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from botwork.config import load_config
from botwork.http import FetchError
from botwork.job_cards import description_pages, handle_job_card
from botwork.jobs import parse_search, search_jobs
from botwork.matching import decide
from botwork.models import Vacancy
from botwork.pipeline import vacancy_ref
from botwork.runner import Monitor
from botwork.sites import length
from botwork.storage import Store
from botwork.telegram import Telegram


class JobCardTests(unittest.TestCase):
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
        self.v = Vacancy('mts', '1', 'Стажёр SOC', '<Компания>', 'https://example.com',
                         work_format='remote', level='unknown', description=('<script> & 😀\n' * 1000))
        self.store.save_snapshot('mts', [(self.v, decide(self.v, self.config['filters']))])
        self.ref = vacancy_ref('mts', '1')

    def message(self, text, owner=123):
        return {'update_id': 1, 'message': {'from': {'id': owner}, 'chat': {'id': owner, 'type': 'private'}, 'text': text}}

    def callback(self, data, owner=123):
        return {'update_id': 2, 'callback_query': {'id': 'dummy', 'from': {'id': owner},
                'message': {'from': {'id': 123}, 'chat': {'id': 123, 'type': 'private'}}, 'data': data}}

    def test_full_description_paging_and_html_safety_without_marking_sent(self):
        pages = description_pages(self.v.description)
        self.assertEqual(''.join(pages), self.v.description)
        for i in range(len(pages)):
            self.telegram.send.reset_mock()
            self.assertTrue(handle_job_card(self.telegram, self.monitor, self.message(f'/job {self.ref} page={i + 1}')))
            for call in self.telegram.send.call_args_list:
                self.assertLess(length(call.args[0]), 3500)
                self.assertNotIn('<script>', call.args[0])
        self.assertEqual(len(self.store.pending(['mts'], 10)), 1)

    def test_closed_and_failed_source_status_are_visible(self):
        self.store.save_snapshot('mts', [])
        self.store.source_error('mts', 'offline')
        handle_job_card(self.telegram, self.monitor, self.message('/job ' + self.ref))
        header = self.telegram.send.call_args_list[0].args[0]
        self.assertIn('Неактивна', header)
        self.assertIn('ошибкой', header)

    def test_card_callbacks_require_owner_and_do_not_mutate_notes(self):
        self.assertFalse(handle_job_card(self.telegram, self.monitor, self.callback('job:' + self.ref + ':1', owner=999)))
        self.telegram.send.assert_not_called()
        self.assertTrue(handle_job_card(self.telegram, self.monitor, self.callback('job:app:' + self.ref)))
        self.assertIn('/note', self.telegram.send.call_args.args[0])
        self.assertEqual(self.store.tracked_vacancies(), [])
        self.telegram.send.side_effect = FetchError('offline')
        with self.assertRaises(FetchError):
            handle_job_card(self.telegram, self.monitor, self.message('/job ' + self.ref))
        self.assertEqual(len(self.store.pending(['mts'], 10)), 1)

    def test_profile_format_and_inferred_level_filters(self):
        rules = deepcopy(self.config['filters'])
        self.config['_search_profiles'] = [{'name': 'ИБ СПб', 'filters': rules | {'allowed_formats': ['hybrid']}},
                                           {'name': 'Удалённо', 'filters': rules | {'allowed_formats': ['remote']}}]
        self.assertEqual(len(search_jobs(self.store, self.config, parse_search('SOC level=intern format=remote profile="Удалённо"'))), 1)
        self.assertEqual(search_jobs(self.store, self.config, parse_search('profile="ИБ СПб"')), [])
        self.assertEqual(search_jobs(self.store, self.config, parse_search('level=junior')), [])
        with self.assertRaises(ValueError):
            search_jobs(self.store, self.config, parse_search('profile="Нет профиля"'))
        for value in ('level=senior', 'format=onsite', 'profile="незакрытая кавычка', 'directions=security'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_search(value)
