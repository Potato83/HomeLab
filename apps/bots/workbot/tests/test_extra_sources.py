from dataclasses import asdict
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from workbot.config import load_config
from workbot.matching import decide
from workbot.models import Vacancy
from workbot.service import scan
from workbot.sources import (ParseError, collect, croc_security_count, experience_level,
    parse_bastion_detail, parse_bastion_list, parse_croc_detail, parse_croc_list)
from workbot.storage import Store
from workbot.telegram import format_message

ROOT = Path(__file__).resolve().parents[1]


def bastion_card(title="Junior SOC", experience="Без опыта", city="Санкт-Петербург", work_format="Гибридный график"):
    exp = f"<span>{experience}</span>" if experience else ""
    return f'''<div class="vacancies__total" data-elt="vacanciesTotal">1 вакансия</div>
    <div class="vacancies__list"><a class="vacancies-card" href="/vacancies/junior-soc">
    <h3 class="vacancies-card__title">{title}</h3><div class="vacancies-card__tag">SOC</div>
    <div class="vacancies-card__info">{exp}<span>{city}</span><span>{work_format}</span></div></a></div>'''


def croc_card(experience="Без опыта", city="Санкт-Петербург", employment="Частичная (стажировка)"):
    return f'''<div class="jsVacanciesSorted"><div class="jsVacanciesListValues">
    <div class="vacancy__card-item"><a href="/vacancies/junior-soc/"><span>Стажёр SOC</span>
    <ul><li>Кибербезопасность</li><li>{employment}</li><li>{city}</li><li>{experience}</li></ul>
    </a></div></div></div>'''


def croc_catalog(count=1):
    return f'''<div class="jsVacanciesListValues"><div class="vacancy__card">
    <div class="vacancy__card-title"><h4>Кибербезопасность</h4><div>{count}</div></div></div></div>'''


class ExtraSourceTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config(str(ROOT / "config.toml"))
        self.rules = self.config["filters"]

    def test_bastion_metadata(self):
        v = parse_bastion_list(bastion_card(), "https://bastion-tech.ru/vacancies")[0]
        self.assertEqual(v.cities, ["Санкт-Петербург"])
        self.assertEqual(v.work_format, "hybrid")
        self.assertEqual(v.level, "junior")
        self.assertTrue(decide(v, self.rules).matched)

    def test_experienced_card_overrides_junior_title(self):
        v = parse_bastion_list(bastion_card(experience="3–6 лет"), "https://bastion-tech.ru/vacancies")[0]
        self.assertEqual(v.level, "experienced")
        self.assertTrue(decide(v, self.rules).matched)

    def test_bastion_missing_experience_not_assumed_junior(self):
        v = parse_bastion_list(bastion_card(title="Специалист SOC", experience=""), "https://bastion-tech.ru/vacancies")[0]
        self.assertEqual(v.cities, ["Санкт-Петербург"])
        self.assertTrue(decide(v, self.rules).matched)

    def test_bastion_office_outside_spb_rejected(self):
        v = parse_bastion_list(bastion_card(city="Москва", work_format="В офисе"), "https://bastion-tech.ru/vacancies")[0]
        self.assertEqual(v.work_format, "office")
        self.assertTrue(decide(v, self.rules).matched)

    def test_remote_travel_conditions_are_visible(self):
        v = parse_bastion_list(bastion_card(city="Россия", work_format="Удаленный график с редкими командировками"), "https://bastion-tech.ru/vacancies")[0]
        decision = decide(v, self.rules)
        self.assertTrue(decision.matched)
        self.assertIn("командировками", format_message({"vacancy": asdict(v), "decision": asdict(decision)}))

    def test_bastion_incomplete_list_is_error(self):
        html = bastion_card().replace('>1 вакансия<', '>2 вакансии<')
        with self.assertRaises(ParseError):
            parse_bastion_list(html, "https://bastion-tech.ru/vacancies")

    def test_bastion_explicit_zero_and_broken_page_differ(self):
        self.assertEqual(parse_bastion_list('<div class="vacancies__total" data-elt="vacanciesTotal">0 вакансий</div>', "https://bastion-tech.ru/vacancies"), [])
        with self.assertRaises(ParseError):
            parse_bastion_list('<html>Ошибка</html>', "https://bastion-tech.ru/vacancies")

    def test_bastion_external_detail_url_rejected(self):
        with self.assertRaises(ParseError):
            parse_bastion_list(bastion_card().replace('/vacancies/junior-soc', 'https://example.org/vacancies/junior-soc'), "https://bastion-tech.ru/vacancies")

    def test_bastion_scope_excludes_benefits_and_contacts(self):
        v = parse_bastion_list(bastion_card(title="Специалист SOC", experience=""), "https://bastion-tech.ru/vacancies")[0]
        html = '''<h1 class="hero__title">Специалист SOC</h1><section id="vacancy-info">
        <div class="vacancy-info__block" data-tab="1"><div class="vacancy-info-card__text">Мониторинг событий</div></div>
        <div class="vacancy-info__block" data-tab="4"><div class="vacancy-info-card__text">Junior рекрутер</div></div></section>
        <section id="vacancy-about">Стажировки, удаленная работа</section>'''
        v = parse_bastion_detail(html, v)
        self.assertNotIn("Junior", v.description)
        self.assertNotIn("Стажировки", v.description)
        self.assertTrue(decide(v, self.rules).matched)

    def test_croc_catalog_and_metadata(self):
        count = croc_security_count(croc_catalog())
        v = parse_croc_list(croc_card(), "https://careers.croc.ru/vacancies/?sections=11", count)[0]
        self.assertEqual(v.cities, ["Санкт-Петербург"])
        self.assertEqual(v.level, "intern")
        self.assertTrue(decide(v, self.rules).matched)

    def test_croc_incomplete_list_is_error(self):
        with self.assertRaises(ParseError):
            parse_croc_list(croc_card(), "https://careers.croc.ru/vacancies/?sections=11", 2)

    def test_croc_remote_not_used_as_city(self):
        v = parse_croc_list(croc_card(city="Удаленно по РФ", employment="Удаленная работа"), "https://careers.croc.ru/vacancies/?sections=11", 1)[0]
        self.assertEqual(v.cities, [])
        self.assertEqual(v.work_format, "remote")
        self.assertTrue(decide(v, self.rules).matched)

    def test_croc_experienced_overrides_intern_tag(self):
        v = parse_croc_list(croc_card(experience="3-6 лет"), "https://careers.croc.ru/vacancies/?sections=11", 1)[0]
        self.assertTrue(decide(v, self.rules).matched)

    def test_croc_description_excludes_other_vacancies(self):
        v = parse_croc_list(croc_card(), "https://careers.croc.ru/vacancies/?sections=11", 1)[0]
        html = '''<div class="vacancy-detail__content-main"><h1>Стажёр SOC</h1>
        <div class="vacancy-detail__content-main-item">Работа в офисе</div></div>
        <div class="vacancy-detail__content-others">Удаленная работа</div>
        <div class="vacancy-detail__content-offers">Гибридный формат</div>'''
        v = parse_croc_detail(html, v)
        self.assertEqual(v.work_format, "office")
        self.assertNotIn("Удаленная", v.description)

    def test_croc_collect_checks_catalog_and_fetches_detail(self):
        settings = self.config['sources']['croc']
        pages = {settings['url']: croc_card(), settings['catalog_url']: croc_catalog(),
                 'https://careers.croc.ru/vacancies/junior-soc/': '<div class="vacancy-detail__content-main"><h1>Стажёр SOC</h1><div class="vacancy-detail__content-main-item">Удаленная работа</div></div>'}
        class Client:
            def get(self, url):
                return pages[url]
        rows = collect('croc', settings, Client(), self.rules)
        self.assertEqual(rows[0].work_format, "remote")

    def test_one_failed_source_does_not_stop_next(self):
        config = self.config
        for name in config['sources']:
            config['sources'][name]['enabled'] = name in ('bastion', 'croc')
        v = Vacancy('croc', '1', 'Junior SOC', 'КРОК', 'https://careers.croc.ru/vacancies/1/', work_format='remote')
        with patch('workbot.service.collect', side_effect=[ParseError('неполный список'), [v]]):
            result = scan(config)
        self.assertIsNotNone(result['sources']['bastion']['error'])
        self.assertEqual(result['sources']['croc']['matched'], 1)

    def test_legacy_payload_without_new_fields_still_formats(self):
        payload = asdict(Vacancy('kaspersky', '1', 'Junior SOC', 'Компания', 'https://example.org'))
        payload.pop('experience')
        payload.pop('work_format_label')

    def test_experience_ranges(self):
        for label, expected in [('Без опыта', 'junior'), ('0-1 год', 'junior'), ('1–3 года', 'unknown'), ('3–6 лет', 'experienced'), ('Более 10 лет', 'experienced'), ('', 'unknown')]:
            with self.subTest(label=label):
                self.assertEqual(experience_level(label), expected)


if __name__ == '__main__':
    unittest.main()
