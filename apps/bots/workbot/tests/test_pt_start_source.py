from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
import tempfile
import unittest

from workbot.config import load_config
from workbot.matching import decide
from workbot.models import normalize
from workbot.program_sources import recruitment_state
from workbot.pt_start_source import parse_pt_training
from workbot.runner import Monitor
from workbot.service import scan
from workbot.sources import ParseError
from workbot.storage import Store
from workbot.telegram import format_message
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
URL = 'https://pt-start.ru/with-education'


def direction(number, name, body):
    return f'''<div class="t-rec" id="heading-{number}"><div class="tn-group content{number}">
    <div class="tn-atom">{name}</div></div></div>
    <div class="t-rec" id="body-{number}"><div class="tn-atom">Чем занимаемся</div>
    <div class="tn-atom">{body}</div></div>'''


def document(hero='Набор заявок завершен 20 августа', application=None, roles=None):
    application = hero if application is None else application
    roles = roles if roles is not None else [
        ('Продуктовая экспертиза', 'Исследуем киберугрозы и создаём правила SIEM.'),
        ('Антивирусная лаборатория', 'Анализируем ВПО.'),
        ('Разработка Python', 'Создаём приложение; основы информационной безопасности будут плюсом.')]
    return f'''<div class="t-rec" id="hero"><div class="tn-atom">Стажировка с обучением</div>
    <div class="tn-atom">{hero}</div></div>
    <div class="t-rec"><a name="steps"></a></div>
    <div class="t-rec"><div class="tn-atom">Офер на оплачиваемую стажировку</div></div>
    <div class="t-rec"><a name="speciality"></a></div>
    {''.join(direction(i + 1, *role) for i, role in enumerate(roles))}
    <div class="t-rec" id="application"><div class="tn-atom">Подать заявку</div>
    <div class="tn-atom">Сделай первый шаг к карьере</div><div class="tn-atom">{application}</div></div>
    <div class="t-rec"><div class="tn-atom">История прошлого года: набор открыт.</div></div>'''


class PtStartTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config(str(ROOT / 'config.toml'))
        self.rules = self.config['filters']

    def test_closed_paid_internship_programme_not_notified(self):
        rows = parse_pt_training(document(), URL, self.rules)
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0].active)
        self.assertFalse(decide(rows[0], self.rules).matched)

    def test_open_programme_is_one_notification_with_clear_notes(self):
        rows = parse_pt_training(document('Набор заявок открыт'), URL, self.rules)
        self.assertEqual(len(rows), 1)
        decision = decide(rows[0], self.rules)
        self.assertTrue(decision.matched)
        self.assertTrue(decision.warnings)
        text = format_message({'vacancy': asdict(rows[0]), 'decision': asdict(decision)})
        self.assertIn('обучение и отбор', text)
        self.assertIn('Антивирусная лаборатория', text)
        self.assertNotIn('Разработка Python', text)

    def test_application_button_alone_does_not_confirm_recruitment(self):
        with self.assertRaises(ParseError):
            parse_pt_training(document('Подать заявку'), URL, self.rules)

    def test_conflicting_status_is_error(self):
        with self.assertRaises(ParseError):
            parse_pt_training(document('Набор заявок открыт', 'Набор заявок завершен'), URL, self.rules)

    def test_education_without_paid_internship_stage_is_rejected(self):
        html = document().replace('Офер на оплачиваемую стажировку', 'Сертификат об обучении')
        with self.assertRaises(ParseError):
            parse_pt_training(html, URL, self.rules)

    def test_paid_offer_phrase_in_footer_is_not_a_stage(self):
        html = document().replace('Офер на оплачиваемую стажировку', 'Сертификат об обучении')
        html += '<div class="tn-atom">Офер на оплачиваемую стажировку</div>'
        with self.assertRaises(ParseError):
            parse_pt_training(html, URL, self.rules)

    def test_negated_paid_offer_not_a_valid_stage(self):
        html = document().replace('Офер на оплачиваемую стажировку', 'Офер на оплачиваемую стажировку не предлагается')
        with self.assertRaises(ParseError):
            parse_pt_training(html, URL, self.rules)

    def test_missing_direction_description_is_error(self):
        html = document().replace('Чем занимаемся', 'Что-то другое', 1)
        with self.assertRaises(ParseError):
            parse_pt_training(html, URL, self.rules)

    def test_missing_structural_anchor_is_error(self):
        for marker in ['name="steps"', 'name="speciality"']:
            with self.subTest(marker=marker), self.assertRaises(ParseError):
                parse_pt_training(document().replace(marker, ''), URL, self.rules)

    def test_development_and_events_are_not_added_as_ib(self):
        roles = [('Разработка React', 'Основы информационной безопасности.'),
                 ('Маркетинг', 'Рекламируем наш продукт.')]
        with self.assertRaises(ParseError):
            parse_pt_training(document(roles=roles), URL, self.rules)

    def test_duplicate_directions_are_collapsed(self):
        role = ('Антивирусная лаборатория', 'Анализируем ВПО.')
        v = parse_pt_training(document(roles=[role, role]), URL, self.rules)[0]
        self.assertEqual(v.notes[1].count('Антивирусная лаборатория'), 1)

    def test_conflicting_duplicate_direction_is_error(self):
        roles = [('Антивирусная лаборатория', 'Анализируем ВПО.'),
                 ('Антивирусная лаборатория', 'Исследуем киберугрозы другим способом.')]
        with self.assertRaises(ParseError):
            parse_pt_training(document(roles=roles), URL, self.rules)

    def test_reopen_notifies_once_and_survives_restart(self):
        closed = parse_pt_training(document(), URL, self.rules)[0]
        opened = parse_pt_training(document('Набор заявок открыт'), URL, self.rules)[0]
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'bot.sqlite3')
            store = Store(path)
            try:
                store.save_snapshot(closed.source, [(closed, decide(closed, self.rules))])
                self.assertFalse(store.pending([closed.source], 20))
                store.save_snapshot(opened.source, [(opened, decide(opened, self.rules))])
                pending = store.pending([opened.source], 20)
                self.assertEqual(len(pending), 1)
                store.mark_sent(pending[0])
            finally:
                store.close()
            store = Store(path)
            try:
                store.save_snapshot(opened.source, [(opened, decide(opened, self.rules))])
                self.assertFalse(store.pending([opened.source], 20))
            finally:
                store.close()

    def test_parser_failure_preserves_snapshot_and_holds_delivery(self):
        opened = parse_pt_training(document('Набор заявок открыт'), URL, self.rules)[0]
        config = deepcopy(self.config)
        for name, settings in config['sources'].items():
            settings['enabled'] = name == 'pt_start_training'
        class Client:
            def get(self, url):
                return '<html>Changed page</html>'
        with tempfile.TemporaryDirectory() as directory:
            store = Store(str(Path(directory) / 'bot.sqlite3'))
            try:
                store.save_snapshot(opened.source, [(opened, decide(opened, self.rules))])
                result = scan(config, store, Client())
                self.assertTrue(result['sources'][opened.source]['error'])
                self.assertEqual(len(store.pending([opened.source], 20)), 1)
                telegram = Mock()
                Monitor(config, store, telegram, delivery_delay=0).dispatch_pending()
                telegram.send.assert_not_called()
            finally:
                store.close()

    def test_legacy_payload_missing_notes_still_formats(self):
        v = parse_pt_training(document('Набор заявок открыт'), URL, self.rules)[0]
        payload = asdict(v)
        payload.pop('notes')
        self.assertIn('PT Start', format_message({'vacancy': payload, 'decision': asdict(decide(v, self.rules))}))

    def test_notes_are_escaped(self):
        v = parse_pt_training(document('Набор заявок открыт'), URL, self.rules)[0]
        v.notes = ['<script>not markup</script>']
        text = format_message({'vacancy': asdict(v), 'decision': asdict(decide(v, self.rules))})
        self.assertNotIn('<script>', text)
        self.assertIn('&lt;script&gt;', text)

    def test_uppercase_yo_is_normalized_before_matching_status(self):
        self.assertEqual(normalize('ЗАВЕРШЁН'), 'завершен')
        self.assertIs(recruitment_state('Набор заявок ЗАВЕРШЁН'), False)


if __name__ == '__main__':
    unittest.main()
