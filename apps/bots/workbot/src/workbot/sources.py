import re
from urllib.parse import urljoin, urlencode
from bs4 import BeautifulSoup
from .matching import contains, infer_format
from .models import Vacancy, normalize
from .limits import MAX_SOURCE_PAGES


class ParseError(RuntimeError):
    pass


def soup(html):
    return BeautifulSoup(html, "html.parser")


def parse_yandex(html, url):
    doc = soup(html)
    result = []
    for item in doc.select(".lc-features__item"):
        heading = item.select_one("h4.card-heading")
        if not heading:
            continue
        card = item.select_one(".card")
        status = item.select_one(".recruitment")
        if not card or not status:
            continue
        name = heading.get_text(" ", strip=True)
        links = [urljoin(url, a["href"]) for a in item.select("a[href]")]
        link = next((x for x in links if x.startswith("https://yandex.ru/yaintern/") and x != url), None)
        if not link:
            # Some genuine directions have only an application form. Their
            # catalogue page remains the source; do not invent a detail URL.
            from hashlib import sha256
            identifier = 'direction-' + sha256(normalize(name).encode()).hexdigest()[:16]
            link = url
        else:
            identifier = link.rstrip('/').rsplit('/', 1)[-1]
        from .program_sources import recruitment_state
        state = recruitment_state(status.get_text(" ", strip=True))
        description = item.select_one(".description")
        result.append(Vacancy("yandex", identifier,
                              f"Стажировка: {name}", "Яндекс", link,
                              description.get_text(" ", strip=True) if description else "",
                              categories=[name], level="intern",
                              active=state is not False,
                              recruitment_status='unknown' if state is None else 'open' if state else 'closed'))
    if not result:
        raise ParseError("Яндекс: не найдены карточки; возможно, изменилась страница")
    return list({v.external_id: v for v in result}.values())


def parse_kaspersky_list(html, base_url):
    doc = soup(html)
    rows = []
    for a in doc.select('h2[data-testid="vacancy-name"] a[href^="/vacancy/"]'):
        card = a.find_parent("div", attrs={"data-testid": re.compile(r"^vacancy-(?!name|tag|body)")})
        if card is None:
            raise ParseError("Kaspersky: не найден контейнер карточки")
        categories = [x.get_text(" ", strip=True) for x in card.select('[data-testid^="vacancy-tag-category-"]')]
        cities = [x.get_text(" ", strip=True) for x in card.select('[data-testid^="vacancy-tag-city-"]')]
        link = urljoin(base_url, a["href"])
        rows.append(Vacancy("kaspersky", link.rsplit("/", 1)[-1], a.get_text(" ", strip=True),
                            "Лаборатория Касперского", link, categories=categories, cities=cities))
    if not rows:
        # Treat unrecognised empty pages as errors instead of silently closing vacancies.
        raise ParseError("Kaspersky: не найдены карточки; страница пуста или изменилась")
    pages = []
    for item in doc.select('[data-testid^="pager-page-"]'):
        match = re.fullmatch(r'pager-page-(\d+)', item.get('data-testid', ''))
        if not match or not 1 <= int(match[1]) <= MAX_SOURCE_PAGES:
            raise ParseError("Kaspersky: некорректный счётчик страниц")
        pages.append(int(match[1]))
    return rows, max(pages, default=1)


def parse_kaspersky_detail(html, vacancy):
    from .structured import enrich_job
    if enrich_job(html, vacancy):
        return vacancy
    doc = soup(html)
    body = doc.select_one('[data-testid="vacancy-body"]')
    title = doc.select_one('[data-testid="vacancy-title"]')
    if not body or not title:
        raise ParseError("Kaspersky: отсутствует описание вакансии")
    vacancy.title = title.get_text(" ", strip=True)
    vacancy.description = body.get_text("\n", strip=True)
    vacancy.work_format = infer_format(vacancy.description)
    return vacancy


def experience_level(text):
    text = normalize(text)
    if re.search(r"без опыта|не требуется|^0\s*-\s*1\s*год", text):
        return "junior"
    match = re.search(r"(?:от\s*)?(\d+)\s*(?:-|лет|год)", text)
    if match and int(match[1]) >= 3:
        return "experienced"
    return "unknown"


def is_experience(text):
    return bool(re.search(r"без опыта|\d.*(?:лет|год)|опыт.*не требуется", normalize(text)))


def detail_url(base_url, href):
    from urllib.parse import urlsplit
    url = urljoin(base_url, href)
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc != urlsplit(base_url).netloc or not parts.path.startswith("/vacancies/"):
        raise ParseError("Некорректная ссылка на вакансию")
    return url


def parse_bastion_list(html, base_url):
    doc = soup(html)
    total = doc.select_one('.vacancies__total[data-elt="vacanciesTotal"]')
    if not total or not (count := re.search(r"\d+", total.get_text())):
        raise ParseError("Бастион: отсутствует число вакансий")
    rows = []
    for card in doc.select('.vacancies__list a.vacancies-card[href]'):
        title = card.select_one('.vacancies-card__title')
        category = card.select_one('.vacancies-card__tag')
        info = card.select_one('.vacancies-card__info')
        if not title or info is None:
            raise ParseError("Бастион: неполная карточка вакансии")
        metadata = [x.get_text(" ", strip=True) for x in info.select('span')]
        experience = metadata.pop(0) if metadata and is_experience(metadata[0]) else ""
        if len(metadata) > 2:
            raise ParseError("Бастион: неизвестная структура условий")
        city = metadata[0] if len(metadata) == 2 else ""
        label = metadata[-1] if metadata else ""
        link = detail_url(base_url, card['href'])
        rows.append(Vacancy("bastion", link.rstrip('/').rsplit('/', 1)[-1],
                            title.get_text(" ", strip=True), "Бастион", link,
                            categories=[category.get_text(" ", strip=True)] if category else [],
                            cities=[city] if city else [], work_format=infer_format(label),
                            work_format_label=label, experience=experience,
                            level=experience_level(experience)))
    rows = list({v.external_id: v for v in rows}.values())
    if len(rows) != int(count[0]):
        raise ParseError("Бастион: получен неполный список вакансий")
    return rows


def parse_bastion_detail(html, vacancy):
    from .structured import enrich_job
    if enrich_job(html, vacancy):
        return vacancy
    doc = soup(html)
    title = doc.select_one('h1.hero__title')
    blocks = doc.select('#vacancy-info .vacancy-info__block[data-tab="1"] .vacancy-info-card__text, '
                        '#vacancy-info .vacancy-info__block[data-tab="2"] .vacancy-info-card__text, '
                        '#vacancy-info .vacancy-info__block[data-tab="3"] .vacancy-info-card__text')
    if not title or not blocks:
        raise ParseError("Бастион: отсутствует описание вакансии")
    vacancy.title = title.get_text(" ", strip=True)
    vacancy.description = "\n".join(x.get_text("\n", strip=True) for x in blocks)
    # The company benefits and recruiter contacts are deliberately excluded.
    if vacancy.work_format == "unknown":
        vacancy.work_format = infer_format(vacancy.description)
    return vacancy


def croc_security_count(html):
    doc = soup(html)
    for group in doc.select('.jsVacanciesListValues .vacancy__card'):
        heading = group.select_one('.vacancy__card-title h4')
        if heading and normalize(heading.get_text()) == "кибербезопасность":
            count = group.select_one('.vacancy__card-title div')
            if count and re.fullmatch(r"\d+", count.get_text(strip=True)):
                return int(count.get_text(strip=True))
    raise ParseError("КРОК: не найден счётчик направления кибербезопасности")


def parse_croc_list(html, base_url, expected_count):
    doc = soup(html)
    container = doc.select_one('.jsVacanciesSorted .jsVacanciesListValues')
    if container is None:
        raise ParseError("КРОК: отсутствует список отфильтрованных вакансий")
    rows = []
    employment = re.compile(r"^(полная занятость|неполная занятость|частичная.*|сменный график)$")
    for card in container.select('.vacancy__card-item > a[href]'):
        title = card.find('span', recursive=False)
        tags = [x.get_text(" ", strip=True) for x in card.select('ul > li')]
        if not title or not tags:
            raise ParseError("КРОК: неполная карточка вакансии")
        category, *metadata = tags
        experience = next((x for x in metadata if is_experience(x)), "")
        formats = [x for x in metadata if infer_format(x) != "unknown"]
        cities = [x for x in metadata if x != experience and x not in formats and not employment.fullmatch(normalize(x))]
        label = "; ".join(formats)
        level = experience_level(experience)
        if level != "experienced" and any("стажиров" in normalize(x) for x in metadata):
            level = "intern"
        link = detail_url(base_url, card['href'])
        rows.append(Vacancy("croc", link.rstrip('/').rsplit('/', 1)[-1],
                            title.get_text(" ", strip=True), "КРОК", link,
                            categories=[category], cities=cities,
                            work_format=infer_format(label), work_format_label=label,
                            experience=experience, level=level))
    rows = list({v.external_id: v for v in rows}.values())
    if len(rows) != expected_count:
        raise ParseError("КРОК: число карточек не совпадает с каталогом; возможна неполная выдача")
    return rows


def parse_croc_detail(html, vacancy):
    from .structured import enrich_job
    if enrich_job(html, vacancy):
        return vacancy
    doc = soup(html)
    main = doc.select_one('.vacancy-detail__content-main')
    title = main.select_one('h1') if main else None
    blocks = main.select('.vacancy-detail__content-main-text-block, .vacancy-detail__content-main-item') if main else []
    if not title or not blocks:
        raise ParseError("КРОК: отсутствует описание вакансии")
    vacancy.title = title.get_text(" ", strip=True)
    vacancy.description = "\n".join(x.get_text("\n", strip=True) for x in blocks)
    if vacancy.work_format == "unknown":
        vacancy.work_format = infer_format(vacancy.description)
    return vacancy


def needs_detail(vacancy, rules):
    # Enrich relevant catalogue/API candidates at every seniority level.
    # Fetching every unrelated detail page can consume the entire scan budget.
    return vacancy.active and contains(rules['security_patterns'],
        ' '.join([vacancy.title, *vacancy.categories, vacancy.description]))


def collect(name, settings, client, rules):
    if name in ('ctftime', 'ods_events'):
        from .events import collect_ctftime, collect_ods
        return (collect_ctftime if name == 'ctftime' else collect_ods)(settings, client, rules)
    if name == 'habr':
        from .new_sources import collect_habr
        return collect_habr(settings, client, rules)
    if name == 'yandex_jobs':
        from .new_sources import collect_yandex_jobs
        return collect_yandex_jobs(settings, client, rules)
    if name == 'hh':
        from .hh_source import collect_hh
        return collect_hh(settings, client, rules)
    if name == 'sber':
        from .sber_source import collect_sber
        return collect_sber(settings, client, rules)
    if name == 'pt_fast_track':
        from .fast_track_source import collect_fast_track
        return collect_fast_track(settings, client, rules)
    if name == "mts":
        from .mts_source import collect_mts
        return collect_mts(settings, client, rules)
    url = settings["url"]
    if name == 'croc_internships':
        from .croc_internships import URL, parse_programmes
        if url != URL:
            raise ParseError('КРОК стажировки: неизвестный адрес программы')
        return parse_programmes(client.get(url))
    if name == 'kaspersky':
        pages = settings.get('max_pages', 30)
        if type(pages) is not int or not 1 <= pages <= MAX_SOURCE_PAGES:
            raise ValueError(f'Kaspersky: max_pages должен быть от 1 до {MAX_SOURCE_PAGES}')
    html = client.get(url)
    if name == 'pt_start_training':
        from .pt_start_source import parse_pt_training
        return parse_pt_training(html, url, rules)
    if name == "x5_internships":
        from .program_sources import parse_x5_internships
        return parse_x5_internships(html, url, rules)
    if name == "yandex":
        return parse_yandex(html, url)
    if name == "bastion":
        rows = parse_bastion_list(html, url)
        return [parse_bastion_detail(client.get(v.url), v) if needs_detail(v, rules) else v for v in rows]
    if name == "croc":
        count = croc_security_count(client.get(settings['catalog_url']))
        rows = parse_croc_list(html, url, count)
        return [parse_croc_detail(client.get(v.url), v) if needs_detail(v, rules) else v for v in rows]
    if name != "kaspersky":
        raise ValueError("Неизвестный источник")
    rows, pages = parse_kaspersky_list(html, url)
    if pages > settings.get("max_pages", 30):
        raise ParseError("Kaspersky: превышен лимит страниц; увеличьте max_pages")
    seen = {v.external_id for v in rows}
    # The UI labels pages from 1, while the actual URL parameter is zero-based.
    for page in range(1, pages):
        other, reported_pages = parse_kaspersky_list(client.get(url + "?" + urlencode({"page": page})), url)
        if reported_pages > pages:
            raise ParseError("Kaspersky: число страниц изменилось; повторим сбор позже")
        new = [v for v in other if v.external_id not in seen]
        if not new:
            raise ParseError("Kaspersky: пагинация вернула повторную страницу")
        rows.extend(new)
        seen.update(v.external_id for v in new)
    # Catalogue metadata is sufficient now that career/location gates are gone.
    return rows
