from unittest import TestCase
from unittest.mock import Mock

from botwork.insights import common_words, handle_insights, insights_text
from botwork.telegram import Telegram


class InsightsTests(TestCase):
    def test_counts_each_word_once_per_title_and_separates_groups(self):
        tracked = [{'title': 'Junior SOC SOC аналитик'}, {'title': 'Intern SOC аналитик'}]
        ignored = [{'title': 'Стажёр аналитик продажи'}, {'title': 'Аналитик продажи'}]
        self.assertEqual(common_words(tracked), [('soc', 2), ('аналитик', 2)])
        store = Mock()
        store.tracked_vacancies.side_effect = lambda ignored=False: ignored_rows if ignored else tracked
        ignored_rows = ignored
        text = insights_text(store)
        self.assertIn('В списке откликов: 2', text)
        self.assertIn('Отмечены как неподходящие: 2', text)
        self.assertIn('продажи (2)', text)
        self.assertEqual(text.count('аналитик (2)'), 2)

    def test_command_only_accepts_owner_private_chat(self):
        telegram = Telegram('token', '123')
        telegram.send = Mock()
        store = Mock()
        store.tracked_vacancies.return_value = []
        update = {'message': {'from': {'id': 123}, 'chat': {'id': 999, 'type': 'private'}, 'text': '/insights'}}
        self.assertFalse(handle_insights(telegram, store, update))
        telegram.send.assert_not_called()
        update['message']['chat']['id'] = 123
        self.assertTrue(handle_insights(telegram, store, update))
        self.assertIn('пока недостаточно повторений', telegram.send.call_args.args[0])
