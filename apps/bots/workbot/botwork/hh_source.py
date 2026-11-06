"""Authenticated, read-only HeadHunter API adapter."""

import json
import math
import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from .http import FetchError
from .matching import contains
from .models import Vacancy, normalize
from .employment import employment_types as parse_employment
from .sources import ParseError, needs_detail, soup

API = 'https://api.hh.ru'
EXPERIENCES = {'noExperience': 'junior', 'between1And3': 'unknown',
               'between3And6': 'experienced', 'moreThan6': 'experienced'}


def valid_token(token):
    return bool(token) and token.isascii() and not any(c.isspace() or ord(c) < 33 or ord(c) == 127 for c in token)


def payload(text):
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        raise ParseError('hh.ru: неизвестный ответ API') from None
    if not isinstance(value, dict) or 'errors' in value:
        raise ParseError('hh.ru: API не вернул данные вакансий')
    return value


def labels(value):
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(x, dict) or not isinstance(x.get('id'), str)
            or not isinstance(x.get('name'), str) for x in value):
        raise ParseError('hh.ru: неизвестная структура меток')
    return value


def security_roles(text, rules):
    categories = payload(text).get('categories')
    if not isinstance(categories, list) or not categories:
        raise ParseError('hh.ru: отсутствует справочник профессиональных ролей')
    found = {}
    for category in categories:
        if not isinstance(category, dict) or not isinstance(category.get('roles'), list):
            raise ParseError('hh.ru: изменился справочник профессиональных ролей')
        for row in labels(category['roles']):
            if not re.fullmatch(r'\d+', row['id']):
                raise ParseError('hh.ru: некорректный ID роли')
            if contains(rules['security_patterns'], row['name']):
                if row['id'] in found and found[row['id']] != row['name']:
                    raise ParseError('hh.ru: противоречивые профессиональные роли')
                found[row['id']] = row['name']
    if not found and rules.get('directions', ['security']) == ['security']:
        raise ParseError('hh.ru: в справочнике не найдены роли ИБ')
    return found


def salary_text(row):
    value = row.get('salary_range') or row.get('salary')
    if value is None:
        return 'не указана'
    if not isinstance(value, dict):
        raise ParseError('hh.ru: неизвестная зарплата')
    lower, upper = value.get('from'), value.get('to')
    if any(x is not None and (type(x) not in (int, float) or not math.isfinite(x) or x < 0) for x in (lower, upper)):
        raise ParseError('hh.ru: некорректная зарплата')
    if lower is None and upper is None:
        return 'не указана'
    if lower is not None and upper is not None and lower > upper:
        raise ParseError('hh.ru: противоречивый диапазон зарплаты')
    currency = value.get('currency') or 'валюта не указана'
    if not isinstance(currency, str):
        raise ParseError('hh.ru: некорректная валюта')
    unit = '₽' if currency in ('RUR', 'RUB') else currency
    number = lambda x: f'{x:,.0f}'.replace(',', ' ')
    amount = (number(lower) if lower == upper else f'от {number(lower)} до {number(upper)}') if lower is not None and upper is not None else f'от {number(lower)}' if lower is not None else f'до {number(upper)}'
    suffix = []
    for field in ('mode', 'frequency'):
        entry = value.get(field)
        if entry is not None:
            if not isinstance(entry, dict) or not isinstance(entry.get('name'), str):
                raise ParseError('hh.ru: неизвестный период зарплаты')
            suffix.append(entry['name'])
    if not suffix:
        suffix.append('период не указан')
    if value.get('gross') is True:
        suffix.append('до вычета налогов')
    elif value.get('gross') is False:
        suffix.append('на руки')
    return amount + ' ' + unit + ' · ' + '; '.join(suffix)


def salary_period(row):
    value = row.get('salary_range')
    if not isinstance(value, dict):
        return 'unknown'
    mode, frequency = value.get('mode'), value.get('frequency')
    mode_id = mode.get('id') if isinstance(mode, dict) else None
    frequency_id = frequency.get('id') if isinstance(frequency, dict) else None
    if mode_id:
        return 'monthly' if mode_id == 'MONTH' else 'other'
    if frequency_id:
        return 'monthly' if frequency_id == 'MONTHLY' else 'other'
    return 'unknown'


def parse_vacancy(row):
    if not isinstance(row, dict) or not isinstance(row.get('id'), str) or not re.fullmatch(r'\d+', row['id']):
        raise ParseError('hh.ru: отсутствует ID вакансии')
    title, employer = row.get('name'), row.get('employer')
    if not isinstance(title, str) or not title.strip() or not isinstance(employer, dict) or not isinstance(employer.get('name'), str):
        raise ParseError('hh.ru: отсутствует название или работодатель')
    if type(row.get('archived')) is not bool or type(row.get('closed_for_applicants', False)) is not bool:
        raise ParseError('hh.ru: неизвестный статус вакансии')
    experience = row.get('experience')
    if not isinstance(experience, dict) or experience.get('id') not in EXPERIENCES or not isinstance(experience.get('name'), str):
        raise ParseError('hh.ru: неизвестный опыт работы')
    formats = labels(row.get('work_format'))
    known_formats = {'REMOTE': 'remote', 'HYBRID': 'hybrid', 'ON_SITE': 'office', 'FIELD_WORK': 'field'}
    if any(f['id'] not in known_formats for f in formats):
        raise ParseError('hh.ru: неизвестный формат работы')
    ids = {f['id'] for f in formats}
    work_format = next((known_formats[key] for key in ('REMOTE', 'HYBRID', 'ON_SITE', 'FIELD_WORK') if key in ids), 'unknown')
    # Fallback for older cards without the new work_format field.
    if not formats and row.get('schedule') is not None:
        schedule = row['schedule']
        if not isinstance(schedule, dict) or not isinstance(schedule.get('id'), str):
            raise ParseError('hh.ru: неизвестный график работы')
        if schedule['id'] == 'remote':
            work_format = 'remote'
    area = row.get('area')
    if area is not None and (not isinstance(area, dict) or not isinstance(area.get('name'), str)):
        raise ParseError('hh.ru: неизвестный город')
    description = row.get('description')
    if description is not None and not isinstance(description, str):
        raise ParseError('hh.ru: неизвестное описание')
    employment = row.get('employment')
    if employment is not None and (not isinstance(employment, dict)
            or not isinstance(employment.get('id'), str) or not isinstance(employment.get('name'), str)):
        raise ParseError('hh.ru: неизвестный тип занятости')
    employment_values, employment_known = parse_employment(
        [employment[key] for key in ('id', 'name')] if employment else [])
    salary = salary_text(row)
    salary_value = row.get('salary_range') or row.get('salary') or {}
    level = EXPERIENCES[experience['id']]
    if level != 'experienced' and re.search(r'стаж[её]р|стажиров|\bintern\b', normalize(title)):
        level = 'intern'
    return Vacancy('hh', row['id'], title, employer['name'], 'https://hh.ru/vacancy/' + row['id'],
        description=soup(description).get_text('\n', strip=True) if description else '',
        categories=[r['name'] for r in labels(row.get('professional_roles'))],
        cities=[area['name']] if area and area['name'] else [], work_format=work_format,
        work_format_label='; '.join(f['name'] for f in formats),
        level=level, experience=experience['name'],
        active=not row['archived'] and not row.get('closed_for_applicants', False), salary=salary,
        salary_min=salary_value.get('from'), salary_max=salary_value.get('to'),
        salary_currency=salary_value.get('currency') or '', salary_period=salary_period(row),
        employment_types=employment_values,
        employment_known=employment_known)


def parse_page(text, page, size, allow_depth_split=False):
    data = payload(text)
    numbers = [data.get(key) for key in ('page', 'pages', 'per_page', 'found')]
    if any(type(n) is not int for n in numbers) or not isinstance(data.get('items'), list):
        raise ParseError('hh.ru: неизвестная пагинация')
    current, pages, per_page, found = numbers
    if current != page or per_page != size or found < 0 or pages < 0:
        raise ParseError('hh.ru: API вернул другую страницу или счётчик')
    if found > 2000 and not allow_depth_split:
        raise ParseError('hh.ru: выдача превышает глубину 2000; сузьте поисковые запросы')
    visible = min(found, 2000)
    valid_pages = {math.ceil(found / size), math.ceil(visible / size)}
    if pages not in valid_pages and not (found == 0 and pages in (0, 1)):
        raise ParseError('hh.ru: противоречивые счётчики выдачи')
    if len(data['items']) != min(size, max(0, visible - page * size)):
        raise ParseError('hh.ru: неполная страница')
    rows = [parse_vacancy(row) for row in data['items']]
    if len({r.external_id for r in rows}) != len(rows):
        raise ParseError('hh.ru: повторные ID на странице')
    return rows, (found, pages)


def collect_hh(settings, client, rules):
    api_url = settings['api_url'].rstrip('/')
    if api_url != API:
        raise ValueError('hh.api_url должен быть https://api.hh.ru')
    token = os.environ.get('HH_API_TOKEN', '')
    if not valid_token(token):
        raise FetchError('hh.ru: задайте корректный HH_API_TOKEN в .env; токен не передавайте в чат')
    size, max_pages = settings.get('page_size', 100), settings.get('max_pages', 20)
    lookback_days = settings.get('lookback_days', 365)
    terms = settings.get('search_terms', [])
    if type(size) is not int or not 1 <= size <= 100 or type(max_pages) is not int or not 1 <= max_pages <= 100:
        raise ValueError('hh.ru: некорректные page_size или max_pages')
    if type(lookback_days) is not int or not 30 <= lookback_days <= 1095:
        raise ValueError('hh.ru: lookback_days должен быть от 30 до 1095')
    if not isinstance(terms, list) or len(terms) > 32 or any(not isinstance(t, str) or not 2 <= len(t.strip()) <= 60 for t in terms):
        raise ValueError('hh.ru: некорректные search_terms')
    def get(path):
        try:
            return client.get(API + path, headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json'}, follow_redirects=False)
        except FetchError as exc:
            status = str(exc)
            if status == 'HTTP 403':
                raise FetchError('hh.ru: отказ доступа или капча; проверьте права HH_API_TOKEN') from None
            if status == 'HTTP 401':
                raise FetchError('hh.ru: токен HH_API_TOKEN недействителен или истёк') from None
            if status == 'HTTP 429':
                raise FetchError('hh.ru: лимит запросов; повтор при следующем цикле') from None
            # Do not echo an arbitrary exception from an authenticated request.
            raise FetchError('hh.ru: не удалось прочитать API; проверьте сеть и доступ') from None
    roles = security_roles(get('/professional_roles'), rules)
    if not roles and not terms:
        raise ParseError('hh.ru: нет подходящих ролей или поисковых запросов')
    query_groups = [[('professional_role', key) for key in roles]] if roles else []
    query_groups += [[('text', term), ('search_field', 'name')] for term in dict.fromkeys(normalize(t) for t in terms)]
    request_count = 0
    unique = {}
    for group in query_groups:
        def fetch(page, date_from=None, date_to=None):
            nonlocal request_count
            request_count += 1
            if request_count > 500:
                raise ParseError('hh.ru: превышен лимит 500 запросов за проверку; сузьте поиск')
            query = group + [('experience', 'noExperience'), ('experience', 'between1And3'),
                             ('page', page), ('per_page', size)]
            if date_from is not None:
                query += [('date_from', date_from), ('date_to', date_to)]
            rows, paging = parse_page(get('/vacancies?' + urlencode(query)), page, size, allow_depth_split=True)
            for v in rows:
                if v.level == 'experienced':
                    raise ParseError('hh.ru: API проигнорировал фильтр опыта')
                if group[0][0] == 'professional_role' and not any(name in roles.values() for name in v.categories):
                    raise ParseError('hh.ru: API проигнорировал фильтр ролей')
            return rows, paging

        def complete_window(first, last):
            first_text, last_text = first.isoformat(), last.isoformat()
            rows, paging = fetch(0, first_text, last_text)
            if paging[0] >= 2000 or paging[1] > max_pages:
                if first == last:
                    raise ParseError('hh.ru: выдача за сутки превышает глубину 2000; сузьте поисковый запрос')
                midpoint = first + (last - first) // 2
                return (complete_window(first, midpoint) +
                        complete_window(midpoint + timedelta(days=1), last))
            seen = {v.external_id for v in rows}
            for page in range(1, paging[1]):
                batch, current = fetch(page, first_text, last_text)
                if current != paging or any(v.external_id in seen for v in batch):
                    raise ParseError('hh.ru: временной диапазон изменился или страницы повторились')
                rows.extend(batch)
                seen.update(v.external_id for v in batch)
            return rows

        rows, paging = fetch(0)
        if paging[0] >= 2000 or paging[1] > max_pages:
            end = datetime.now(timezone.utc).date()
            start = end - timedelta(days=lookback_days - 1)
            rows = []
            cursor = end
            while cursor >= start:
                first = max(start, cursor - timedelta(days=29))
                rows.extend(complete_window(first, cursor))
                cursor = first - timedelta(days=1)
        else:
            seen = {v.external_id for v in rows}
            for page in range(1, paging[1]):
                batch, current = fetch(page)
                if current != paging or any(v.external_id in seen for v in batch):
                    raise ParseError('hh.ru: выдача изменилась или страницы повторились')
                rows.extend(batch)
                seen.update(v.external_id for v in batch)
        for v in rows:
            if v.external_id in unique and unique[v.external_id] != v:
                raise ParseError('hh.ru: противоречивые версии вакансии')
            unique[v.external_id] = v
    result = []
    for v in unique.values():
        if v.active and needs_detail(v, rules):
            detail = payload(get('/vacancies/' + v.external_id))
            current = parse_vacancy(detail)
            if current.external_id != v.external_id or not current.description:
                raise ParseError('hh.ru: нет полного собственного описания вакансии')
            v = current
        result.append(v)
    return result
