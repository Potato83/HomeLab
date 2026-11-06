from pathlib import Path
import tempfile
import unittest

from workbot.config import load_config
from workbot.matching import decide
from workbot.program_sources import parse_x5_internships, recruitment_state
from workbot.sources import ParseError
from workbot.storage import Store

ROOT = Path(__file__).resolve().parents[1]
URL = 'https://generation.x5.ru/internships'


def document(hero='Набор закрыт', registration='Набор на стажировку закрыт', duplicate=False):
    card = '''<article class="rl-card"><h3 class="rl-title">Антифрод и информационная безопасность</h3>
    <p class="rl-text">Анализ инцидентов</p><a href="/internships#registration">Подробнее</a></article>'''
    return f'''<main><section><h1>Стажировка в Х5</h1><a>{hero}</a></section>
    {card}{card if duplicate else ''}<article class="rl-card"><h3 class="rl-title">Маркетинг</h3></article>
    <section id="registration"><h2>{registration}</h2></section></main>
    <aside>В прошлом году набор открыт, пришли на хакатон</aside>'''


class ProgrammeTests(unittest.TestCase):
    def setUp(self):
        self.rules = load_config(str(ROOT / 'config.toml'))['filters']

    def test_closed_recruitment_ignores_historical_text(self):
        v = parse_x5_internships(document(), URL, self.rules)[0]
        self.assertFalse(v.active)
        self.assertFalse(decide(v, self.rules).matched)

    def test_open_recruitment_unknown_location_warned(self):
        v = parse_x5_internships(document('Набор открыт', 'Набор на стажировку открыт'), URL, self.rules)[0]
        d = decide(v, self.rules)
        self.assertTrue(d.matched)
        self.assertTrue(d.warnings)

    def test_desktop_mobile_copies_deduplicated(self):
        self.assertEqual(len(parse_x5_internships(document(duplicate=True), URL, self.rules)), 1)

    def test_conflicting_or_unknown_status_is_error(self):
        for hero, registration in [('Набор открыт', 'Набор закрыт'), ('Подать заявку', 'Спасибо за интерес')]:
            with self.subTest(hero=hero), self.assertRaises(ParseError):
                parse_x5_internships(document(hero, registration), URL, self.rules)

    def test_no_ib_direction_is_error(self):
        with self.assertRaises(ParseError):
            parse_x5_internships(document().replace('Антифрод и информационная безопасность', 'Финансы'), URL, self.rules)

    def test_changed_page_and_external_application_url_are_error(self):
        for html in ['<main>Ошибка</main>', document().replace('/internships#registration', 'https://example.org/internships#registration')]:
            with self.subTest(html=html[:30]), self.assertRaises(ParseError):
                parse_x5_internships(html, URL, self.rules)

    def test_reopen_notifies_once_then_preserves_history(self):
        closed = parse_x5_internships(document(), URL, self.rules)[0]
        opened = parse_x5_internships(document('Набор открыт', 'Набор на стажировку открыт'), URL, self.rules)[0]
        with tempfile.TemporaryDirectory() as directory:
            store = Store(str(Path(directory) / 'bot.sqlite3'))
            try:
                store.save_snapshot(closed.source, [(closed, decide(closed, self.rules))])
                self.assertFalse(store.pending([closed.source], 20))
                store.save_snapshot(opened.source, [(opened, decide(opened, self.rules))])
                rows = store.pending([opened.source], 20)
                self.assertEqual(len(rows), 1)
                store.mark_sent(rows[0])
                store.save_snapshot(opened.source, [(opened, decide(opened, self.rules))])
                self.assertFalse(store.pending([opened.source], 20))
            finally:
                store.close()


if __name__ == '__main__':
    unittest.main()
