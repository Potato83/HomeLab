from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from botwork.config import load_config
from botwork.http import FetchError, telegram_call
from botwork.matching import decide, infer_format
from botwork.models import Vacancy
from botwork.service import scan
from botwork.sources import ParseError, collect, parse_kaspersky_detail, parse_kaspersky_list, parse_yandex
from botwork.storage import Store
from botwork.telegram import Telegram, deliver, format_message

ROOT = Path(__file__).resolve().parents[1]


def vacancy(**kwargs):
    values = dict(source="kaspersky", external_id="123", title="Стажёр по информационной безопасности", company="Компания", url="https://example.org/vacancy/123", cities=["Санкт-Петербург"], work_format="hybrid")
    return Vacancy(**(values | kwargs))


def kas_list(identifier="123", title="Стажёр SOC", pages=1):
    return f'''<div data-testid="vacancy-{identifier}"><h2 data-testid="vacancy-name"><a href="/vacancy/{identifier}">{title}</a></h2><span data-testid="vacancy-tag-category-1">Информационная безопасность</span><span data-testid="vacancy-tag-city-1">Санкт-Петербург</span></div><button data-testid="pager-page-{pages}">{pages}</button>'''


def yandex(status="Набор открыт"):
    return f'''<section class="lc-features__item"><div class="card"><div class="recruitment">{status}</div><h4 class="card-heading">Security Engineering</h4><p class="description">Работа с безопасностью</p></div><a href="https://yandex.ru/yaintern/security">Подробнее</a></section>'''


class MatchingTests(unittest.TestCase):
    def setUp(self):
        self.rules = load_config(str(ROOT / "config.toml"))["filters"]

    def test_locations(self):
        cases = [
            ({}, True),
            ({"cities": ["Москва"], "work_format": "office"}, False),
            ({"cities": ["Москва"], "work_format": "hybrid"}, False),
            ({"cities": ["Владивосток"], "work_format": "remote"}, True),
            ({"cities": [], "work_format": "unknown"}, True),
            ({"cities": ["Москва"], "work_format": "unknown"}, True),
            ({"cities": ["Санкт‑Петербург"], "work_format": "office"}, True),
        ]
        for values, expected in cases:
            with self.subTest(values=values):
                self.assertEqual(decide(vacancy(**values), self.rules).matched, expected)
        self.assertTrue(decide(vacancy(cities=[], work_format="unknown"), self.rules).warnings)

    def test_junior_selected_directions(self):
        for title, expected in [("Junior AppSec", True), ("Стажер в команду по анализу защищенности", True), ("Младший аналитик вредоносного ПО", True), ("Стажер SOC", True), ("Junior DevOps", True), ("MLOps intern", True), ("Senior SOC Analyst", False), ("Middle Security Engineer", False), ("Security Engineer", False)]:
            with self.subTest(title=title):
                self.assertEqual(decide(vacancy(title=title), self.rules).matched, expected)

    def test_no_company_text_security_match(self):
        self.assertFalse(decide(vacancy(title="Junior Python", description="Наша компания занимается информационной безопасностью"), self.rules).matched)

    def test_closed(self):
        self.assertFalse(decide(vacancy(active=False), self.rules).matched)

    def test_format_negation_and_hybrid(self):
        self.assertEqual(infer_format("Гибрид, удалённо 2 дня в неделю"), "hybrid")
        self.assertEqual(infer_format("Удаленная работа не предусмотрена"), "office")
        self.assertEqual(infer_format("Удаленная работа"), "remote")


class ParserTests(unittest.TestCase):
    def test_yandex_closed_not_lured_by_application_link(self):
        html = yandex("Набор закрыт").replace("</section>", '<a href="https://forms.yandex.ru/test">Подать заявку</a></section>')
        v = parse_yandex(html, "https://yandex.ru/yaintern/internship")[0]
        self.assertFalse(v.active)
        self.assertEqual(v.external_id, "security")

    def test_yandex_open(self):
        self.assertTrue(parse_yandex(yandex(), "https://yandex.ru/yaintern/internship")[0].active)

    def test_changed_markup_is_error(self):
        for parser, url in [(parse_yandex, "https://yandex.ru"), (parse_kaspersky_list, "https://careers.kaspersky.ru")]:
            with self.assertRaises(ParseError):
                parser("<html>Captcha or changed page</html>", url)

    def test_kaspersky_metadata_and_detail_scope(self):
        rows, pages = parse_kaspersky_list(kas_list(pages=3), "https://careers.kaspersky.ru/vacancies")
        self.assertEqual(pages, 3)
        self.assertEqual(rows[0].cities, ["Санкт-Петербург"])
        html = '<h1 data-testid="vacancy-title">Стажёр SOC</h1><div data-testid="vacancy-body">Гибрид в СПб</div><div>FAQ: Удалённая работа в Москве, опыт не требуется</div>'
        v = parse_kaspersky_detail(html, rows[0])
        self.assertEqual(v.work_format, "hybrid")
        self.assertNotIn("FAQ", v.description)

    def test_pagination_all_pages(self):
        config = load_config(str(ROOT / "config.toml"))
        pages = {"https://careers.kaspersky.ru/vacancies": kas_list("1", "Senior SOC", 2), "https://careers.kaspersky.ru/vacancies?page=1": kas_list("2", "Senior AppSec", 2)}
        class Client:
            def get(self, url):
                return pages[url]
        result = collect("kaspersky", config["sources"]["kaspersky"], Client(), config["filters"])
        self.assertEqual([v.external_id for v in result], ["1", "2"])

    def test_repeated_page_is_error(self):
        config = load_config(str(ROOT / "config.toml"))
        class Client:
            def get(self, url):
                return kas_list("1", "Senior SOC", 2)
        with self.assertRaises(ParseError):
            collect("kaspersky", config["sources"]["kaspersky"], Client(), config["filters"])


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "bot.sqlite3")
        self.store = Store(self.path)
        self.rules = load_config(str(ROOT / "config.toml"))["filters"]

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def save(self, v=None):
        v = v or vacancy()
        self.store.save_snapshot(v.source, [(v, decide(v, self.rules))])

    def test_initial_and_restart_no_duplicate(self):
        self.save()
        row = self.store.pending(["kaspersky"], 20)[0]
        self.store.mark_sent(row)
        self.store.close()
        self.store = Store(self.path)
        self.save()
        self.assertEqual(self.store.pending(["kaspersky"], 20), [])

    def test_failed_send_is_retried(self):
        self.save()
        class Failing:
            def send(self, text, reply_markup=None):
                raise FetchError("network")
        with self.assertRaises(FetchError):
            deliver(self.store, Failing(), ["kaspersky"], 20, delay=0)
        self.assertEqual(len(self.store.pending(["kaspersky"], 20)), 1)
        from unittest.mock import Mock
        good = Mock()
        self.assertEqual(deliver(self.store, good, ["kaspersky"], 20, delay=0), 1)
        self.assertEqual(self.store.pending(["kaspersky"], 20), [])

    def test_closed_and_reopened(self):
        self.save()
        self.store.mark_sent(self.store.pending(["kaspersky"], 20)[0])
        self.save(vacancy(active=False))
        self.save()
        self.assertEqual(len(self.store.pending(["kaspersky"], 20)), 1)

    def test_source_error_does_not_close_or_overwrite_success(self):
        self.save()
        before = self.store.status()[0]["last_success"]
        self.store.source_error("kaspersky", "HTTP 503")
        self.assertEqual(len(self.store.pending(["kaspersky"], 20)), 1)
        self.assertEqual(self.store.status()[0]["last_success"], before)

    def test_removed_and_disabled_not_sent(self):
        self.save()
        self.assertEqual(self.store.pending(["yandex"], 20), [])
        self.store.save_snapshot("kaspersky", [])
        self.assertEqual(self.store.pending(["kaspersky"], 20), [])

    def test_no_partial_snapshot_on_parser_failure(self):
        self.save()
        config = load_config(str(ROOT / "config.toml"))
        config["sources"]["yandex"]["enabled"] = False
        with patch("botwork.service.collect", side_effect=ParseError("changed")):
            result = scan(config, self.store)
        self.assertTrue(result["sources"]["kaspersky"]["error"])
        self.assertEqual(len(self.store.pending(["kaspersky"], 20)), 1)

    def test_cycle_lock(self):
        other = Store(self.path)
        try:
            with self.store.cycle_lock():
                with self.assertRaises(RuntimeError):
                    with other.cycle_lock():
                        pass
        finally:
            other.close()


class TelegramTests(unittest.TestCase):
    def test_owner_only(self):
        tg = Telegram("test", "42")
        self.assertEqual(tg.command({"message": {"from": {"id": 42}, "chat": {"id": 42, "type": "private"}, "text": "/status"}}), "/status")
        self.assertIsNone(tg.command({"message": {"from": {"id": 42}, "chat": {"id": 43, "type": "private"}, "text": "/check"}}))
        self.assertIsNone(tg.command({"message": {"from": {"id": 42}, "chat": {"id": 42, "type": "group"}, "text": "/check"}}))

    def test_html_escaped(self):
        from dataclasses import asdict
        v = vacancy(title="Junior SOC <script>")
        text = format_message({"vacancy": asdict(v), "decision": {"reason": "ИБ", "warnings": []}})
        self.assertNotIn("<script>", text)
        self.assertIn("&lt;script&gt;", text)

    def test_token_not_exposed_in_error(self):
        from urllib.error import URLError
        with patch("botwork.http.urlopen", side_effect=URLError("https://api.telegram.org/botSECRET/sendMessage")):
            with self.assertRaises(FetchError) as error:
                telegram_call("SECRET", "sendMessage", {})
        self.assertNotIn("SECRET", str(error.exception))


if __name__ == "__main__":
    unittest.main()
