"""Public Habr Career catalogue and Yandex career publications."""
import json
import math
import re
from urllib.parse import urljoin, urlsplit, urlencode
from .models import Vacancy, normalize
from .matching import infer_format
from .sources import ParseError, soup, needs_detail
from .limits import MAX_SOURCE_PAGES


def habr_state(html):
    candidates = []
    for script in soup(html).select('script[type="application/json"]'):
        try:
            data = json.loads(script.get_text())
        except ValueError:
            continue
        if isinstance(data, dict) and isinstance(data.get('vacancies'), dict) and 'meta' in data['vacancies']:
            candidates.append(data)
    if len(candidates) != 1:
        raise ParseError('Хабр Карьера: не найден полный снимок выдачи')
    return candidates[0]


def habr_page(html, page, query=None):
    data = habr_state(html)
    try:
        meta = data['vacancies']['meta']
        total, size, current, pages = [meta[k] for k in ('totalResults', 'perPage', 'currentPage', 'totalPages')]
        rows = data['vacancies']['list']
        search = data['search']
        selected = search['filters']['s']
    except (KeyError, TypeError):
        raise ParseError('Хабр Карьера: изменилась структура выдачи') from None
    if not isinstance(search, dict) or not isinstance(search.get('filters'), dict):
        raise ParseError('Хабр Карьера: изменилась структура поискового запроса')
    echoed_query = search.get('query', '')
    if query is not None and not isinstance(echoed_query, str):
        raise ParseError('Хабр Карьера: некорректный поисковый запрос в ответе')
    if any(type(n) is not int for n in (total, size, current, pages)) or size <= 0 or total < 0 or current != page or not isinstance(rows, list) or not isinstance(selected, list) or (query is None and not selected):
        raise ParseError('Хабр Карьера: некорректный фильтр или пагинация')
    if query is not None and (normalize(echoed_query) != normalize(query) or search.get('order') != 'date'):
        raise ParseError('Хабр Карьера: сайт не подтвердил поисковый запрос')
    if pages != math.ceil(total / size) and not (total == 0 and pages in (0, 1)):
        raise ParseError('Хабр Карьера: выдача ограничена или счётчики противоречат друг другу')
    if len(rows) != min(size, max(0, total - (page - 1) * size)):
        raise ParseError('Хабр Карьера: неполная страница')
    result = []
    for row in rows:
        try:
            identifier, title = row['id'], row['title']
            if type(identifier) is not int or identifier <= 0 or not isinstance(title, str) or not title.strip() or row['href'] != '/vacancies/' + str(identifier):
                raise ValueError
            if type(row['remoteWork']) is not bool or type(row['archived']) is not bool or type(row['hidden']) is not bool:
                raise ValueError
            company = row['company']['title']
            places = row['locations']
            if places is not None and not isinstance(places, list):
                raise ValueError
            locations = [x['title'] for x in places or []]
            categories = [x['title'] for x in row['divisions']]
            if not isinstance(company, str) or any(not isinstance(x, str) for x in [*locations, *categories]):
                raise ValueError
            grade = row['qualification']
            if grade not in (None, 'Intern', 'Junior', 'Middle', 'Senior', 'Lead'):
                raise ValueError
            salary = row['salary']['formatted'] if row.get('salary') is not None else ''
            if not isinstance(salary, str):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise ParseError('Хабр Карьера: неизвестная структура карточки') from None
        result.append(Vacancy('habr', str(identifier), title, company,
            'https://career.habr.com/vacancies/' + str(identifier), categories=categories,
            cities=locations, level=grade.lower() if grade else 'unknown',
            work_format='remote' if row['remoteWork'] else 'unknown',
            work_format_label='Можно удалённо' if row['remoteWork'] else '', salary=salary or 'не указана',
            active=not row['archived'] and not row['hidden']))
    if len({v.external_id for v in result}) != len(result):
        raise ParseError('Хабр Карьера: повторные карточки на странице')
    return result, (total, size, pages, tuple(selected))


def collect_habr(settings, client, rules):
    from .directions import search_terms, selected
    queries = ([None] if 'security' in selected(rules) else []) + search_terms(rules)
    unique = {}
    for query in queries:
        for v in collect_habr_query(settings, client, rules, query):
            if v.external_id in unique and unique[v.external_id] != v:
                raise ParseError('Хабр Карьера: карточка изменилась между запросами')
            unique[v.external_id] = v
    for v in unique.values():
        if v.active and needs_detail(v, rules):
            doc = soup(client.get(v.url))
            title, body = doc.select_one('h1'), doc.select_one('.vacancy-description__text')
            if not title or not body or normalize(title.get_text(' ', strip=True)) != normalize(v.title):
                raise ParseError('Хабр Карьера: нет собственного описания или изменилось название')
            v.description = body.get_text('\n', strip=True)
            if v.work_format == 'unknown':
                v.work_format = infer_format(v.description)
    return list(unique.values())


def collect_habr_query(settings, client, rules, query):
    max_pages = settings.get('max_pages', 30)
    if type(max_pages) is not int or not 1 <= max_pages <= MAX_SOURCE_PAGES:
        raise ValueError(f'Хабр Карьера: max_pages должен быть от 1 до {MAX_SOURCE_PAGES}')
    url = settings['url']
    if query is not None:
        url = 'https://career.habr.com/vacancies?' + urlencode({'q': query, 'type': 'all', 'sort': 'date'})
    if urlsplit(url).netloc != 'career.habr.com':
        raise ParseError('Хабр Карьера: неизвестный хост')
    rows, meta = habr_page(client.get(url), 1, query)
    if meta[2] > max_pages:
        raise ParseError('Хабр Карьера: превышен max_pages')
    seen = {v.external_id for v in rows}
    for page in range(2, meta[2] + 1):
        batch, current = habr_page(client.get(url + ('&' if '?' in url else '?') + urlencode({'page': page, 'type': 'all'})), page, query)
        if current != meta or any(v.external_id in seen for v in batch):
            raise ParseError('Хабр Карьера: выдача изменилась или страницы повторились')
        rows.extend(batch)
        seen.update(v.external_id for v in batch)
    return rows


def yandex_page(text):
    try:
        data = json.loads(text)
        total, next_url, rows = data['count'], data.get('next'), data['results']
    except (ValueError, KeyError, TypeError):
        raise ParseError('Яндекс вакансии: неизвестный ответ API') from None
    if type(total) is not int or total < 0 or not isinstance(rows, list) or (next_url is not None and not isinstance(next_url, str)):
        raise ParseError('Яндекс вакансии: неизвестные счётчики')
    if next_url and not rows:
        raise ParseError('Яндекс вакансии: пустая промежуточная страница')
    result = []
    for row in rows:
        try:
            identifier, title, slug = row['id'], row['title'], row['publication_slug_url']
            if type(identifier) is not int or identifier <= 0 or not isinstance(title, str) or not title.strip() or not isinstance(slug, str) or not re.fullmatch(r'[a-zA-Z0-9_-]+', slug):
                raise ValueError
            vacancy = row['vacancy']
            cities = [c['name'] for c in vacancy['cities']]
            formats = vacancy['work_modes']
            modes = [f['slug'] for f in formats]
            if any(mode not in ('remote', 'mixed', 'office') for mode in modes):
                raise ValueError
            if row['status'] not in ('published', 'archived', 'closed'):
                raise ValueError
            service = row.get('public_service')
            category = service['name'] if service else ''
            summary = row.get('short_summary') or ''
            if not isinstance(summary, str) or not isinstance(category, str) or any(not isinstance(c, str) for c in cities):
                raise ValueError
        except (KeyError, ValueError, TypeError):
            raise ParseError('Яндекс вакансии: изменилась структура карточки') from None
        work_format = next((fmt for mode, fmt in [('remote', 'remote'), ('mixed', 'hybrid'), ('office', 'office')] if mode in modes), 'unknown')
        result.append(Vacancy('yandex_jobs', str(identifier), title, 'Яндекс',
            'https://yandex.ru/jobs/vacancies/' + slug, description=summary,
            categories=[category] if category else [], cities=cities, work_format=work_format,
            work_format_label='; '.join(f['name'] for f in formats), active=row['status'] == 'published'))
    if len({v.external_id for v in result}) != len(result):
        raise ParseError('Яндекс вакансии: повторные карточки')
    return result, total, next_url


def collect_yandex_jobs(settings, client, rules):
    max_pages = settings.get('max_pages', 50)
    if type(max_pages) is not int or not 1 <= max_pages <= MAX_SOURCE_PAGES:
        raise ValueError(f'Яндекс вакансии: max_pages должен быть от 1 до {MAX_SOURCE_PAGES}')
    base = 'https://yandex.ru'
    url = settings['api_url']
    rows, seen, urls, expected = [], set(), set(), None
    for _ in range(max_pages):
        parts = urlsplit(url)
        if parts.scheme != 'https' or parts.netloc != 'yandex.ru' or parts.path != '/jobs/api/jobs/publications/' or parts.fragment:
            raise ParseError('Яндекс вакансии: неизвестный адрес следующей страницы')
        if url in urls:
            raise ParseError('Яндекс вакансии: цикл пагинации')
        urls.add(url)
        batch, total, next_url = yandex_page(client.get(url))
        if expected is None:
            expected = total
        if total != expected or any(v.external_id in seen for v in batch):
            raise ParseError('Яндекс вакансии: выдача изменилась или страницы повторились')
        rows.extend(batch)
        seen.update(v.external_id for v in batch)
        if not next_url:
            break
        url = urljoin(base, next_url)
    else:
        raise ParseError('Яндекс вакансии: превышен max_pages')
    if len(rows) != expected:
        raise ParseError('Яндекс вакансии: неполная выдача')
    for v in rows:
        if v.active and needs_detail(v, rules):
            doc = soup(client.get(v.url))
            title = doc.select_one('h1')
            sections = []
            for heading in doc.select('h2'):
                if normalize(heading.get_text(' ', strip=True)) in ('какие задачи вас ждут', 'мы ждем, что вы'):
                    sections.append(heading.parent.get_text('\n', strip=True))
            if not title or normalize(title.get_text(' ', strip=True)) != normalize(v.title) or len(sections) != 2:
                raise ParseError('Яндекс вакансии: не найдены собственные задачи и требования')
            v.description += '\n' + '\n'.join(sections)
    return rows
