from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from workbot.models import Decision, Vacancy
from workbot.pipeline import handle_feedback, handle_track, track_text, vacancy_ref
from workbot.storage import Store


class IgnoreVacancyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(str(Path(self.temp.name) / 'db'))
        self.addCleanup(self.store.close)
        self.vacancy = Vacancy('mts', '1', 'Стажёр SOC', 'МТС', 'https://example.com', level='intern', work_format='remote')
        self.ref = vacancy_ref('mts', '1')
        self.store.save_snapshot('mts', [(self.vacancy, Decision(True, 'подходит'))])

    def test_ignore_survives_snapshot_and_restore_releases_pending(self):
        track_text(self.store, self.ref + ' ignored')
        self.assertEqual(self.store.pending(['mts'], 10), [])
        self.assertEqual(self.store.pending_counts(['mts']), {})
        self.store.save_snapshot('mts', [(self.vacancy, Decision(True, 'подходит'))])
        self.assertEqual(self.store.pending(['mts'], 10), [])
        self.assertEqual(len(self.store.tracked_vacancies(ignored=True)), 1)
        self.assertEqual(self.store.tracked_vacancies(), [])
        track_text(self.store, self.ref + ' remove')
        self.assertEqual(len(self.store.pending(['mts'], 10)), 1)

    def test_ignored_list_and_saved_status_restore(self):
        track_text(self.store, self.ref + ' ignored')
        telegram = Mock()
        telegram.command.return_value = '/track'
        telegram.arguments.return_value = 'ignored'
        self.assertTrue(handle_track(telegram, self.store, {}))
        self.assertIn(self.ref, telegram.send.call_args.args[0])
        track_text(self.store, self.ref + ' saved')
        self.assertEqual(len(self.store.pending(['mts'], 10)), 1)
        self.assertEqual(self.store.tracked_vacancies()[0]['status'], 'saved')

    def test_callback_owner_and_replayed_update(self):
        from workbot.telegram import Telegram
        telegram = Telegram('dummy', '123')
        telegram.answer_callback = Mock()
        callback = {'id': 'callback', 'from': {'id': 123}, 'message': {'chat': {'id': 123, 'type': 'private'}}, 'data': 'bw:i:' + self.ref}
        update = {'update_id': 1, 'callback_query': callback}
        self.assertTrue(handle_feedback(telegram, self.store, update))
        self.assertEqual(self.store.pending(['mts'], 10), [])
        track_text(self.store, self.ref + ' saved')
        handle_feedback(telegram, self.store, update)
        self.assertEqual(self.store.tracked_vacancies()[0]['status'], 'saved')
        callback['from'] = {'id': 999}
        callback['data'] = 'bw:i:' + self.ref
        handle_feedback(telegram, self.store, {'update_id': 2, 'callback_query': callback})
        self.assertEqual(self.store.tracked_vacancies()[0]['status'], 'saved')

    def test_buttons_in_individual_and_digest_delivery(self):
        from workbot.telegram import deliver, deliver_digest
        telegram = Mock()
        self.assertEqual(deliver(self.store, telegram, ['mts'], 10, delay=0), 1)
        buttons = telegram.send.call_args.kwargs['reply_markup']['inline_keyboard'][0]
        self.assertEqual([button['callback_data'] for button in buttons], ['bw:s:' + self.ref, 'bw:i:' + self.ref])
        self.store.db.execute('UPDATE vacancies SET sent_generation=0')
        self.store.db.commit()
        telegram.reset_mock()
        self.assertEqual(deliver_digest(self.store, telegram, ['mts'], 10, delay=0), 1)
        self.assertEqual(telegram.send.call_args.kwargs['reply_markup']['inline_keyboard'][0][1]['callback_data'], 'bw:i:' + self.ref)
