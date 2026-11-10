"""Sber public career API: IB categories plus paginated keyword searches."""

import json
import math
import re
from urllib.parse import urlencode, urlsplit

from .matching import contains, infer_format
from .models import Vacancy, normalize
from .limits import MAX_SOURCE_PAGES
from .sources import ParseError, soup


def response_data(text):
    try:
        payload = json.loads(text)
        if not isinstance(payload, dict) or payload.get('success') is not True:
            raise ValueError
        return payload['data']
    except (ValueError, KeyError, TypeError):
        raise ParseError('Сбер: неизвестный или неуспешный ответ API') from None


def parse_dictionary(text):
    rows = response_data(text)
    if not isinstance(rows, list) or not rows:
        raise ParseError('Сбер: отсутствует справочник условий')
    result = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ParseError('Сбер: некорректный элемент справочника')
        identifier, label = row.get('id'), row.get('value')
        valid_id = (type(identifier) is int and identifier > 0) or (isinstance(identifier, str) and bool(identifier.strip()))
        if not valid_id or not isinstance(label, str) or not label.strip() or type(row.get('isActive')) is not bool:
            raise ParseError('Сбер: неизвестная структура справочника')
        if str(identifier) in result:
            raise ParseError('Сбер: повторный идентификатор в справочнике')
        result[str(identifier)] = label
    return result


def text_field(row, name):
    value = row.get(name)
    if value is None:
        return ''
    if not isinstance(value, str):
        raise ParseError('Сбер: некорректное текстовое поле вакансии')
    return soup(value).get_text('\n', strip=True)


def reference(row, name, dictionary):
    value = row.get(name)
    if value is None:
        return ''
    if type(value) not in (str, int) or str(value) not in dictionary:
        raise ParseError('Сбер: неизвестный идентификатор условий вакансии')
    return dictionary[str(value)]


def public_slug(title):
    # Same transliteration used by the public search cards. IDs, not slugs,
    # identify a vacancy in our database, so a title edit does not resend it.
    cyrillic = dict(zip('абвгдеёжзийклмнопрстуфхцчшщъыьэюя',
        ['a', 'b', 'v', 'g', 'd', 'e', 'e', 'zh', 'z', 'i', 'y', 'k', 'l', 'm', 'n', 'o',
         'p', 'r', 's', 't', 'u', 'f', 'h', 'c', 'ch', 'sh', 'sch', '', 'y', '', 'e', 'yu', 'ya']))
    parts = [cyrillic.get(char) or (char if 'a' <= char <= 'z' else ' ') for char in title.lower()]
    return re.sub(r'-+', '-', ''.join(parts).strip().replace(' ', '-')) or 'vacancy'


def salary_text(row):
    lower, upper = row.get('salary_min'), row.get('salary_max')
    for value in (lower, upper):
        if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value < 0):
            raise ParseError('Сбер: некорректная зарплата')
    if lower is not None and upper is not None and lower > upper:
        raise ParseError('Сбер: противоречивый диапазон зарплаты')
    amount = lambda value: f'{value:,.0f}'.replace(',', ' ')
    # This API has no currency field. Do not infer one from the employer/city.
    if lower is None and upper is None:
        return 'не указана'
    unit = ' (валюта не указана)'
    if lower is not None and upper is not None:
        return (amount(lower) if lower == upper else f'от {amount(lower)} до {amount(upper)}') + unit
    return (f'от {amount(lower)}' if lower is not None else f'до {amount(upper)}') + unit


def work_conditions(schedule, conditions):
    text = normalize(conditions)
    work_format = infer_format(text)
    # "Удалёнка" is not covered by the common long-form matcher.
    if work_format == 'unknown' and re.search(r'\bудаленка\b', text) and not re.search(r'\bне\b|невозмож', text):
        work_format = 'remote'
    office_amenity = bool(re.search(r'(?:современн\w*\s+)?офис\s+(?:в\b|по адресу)', text))
    if work_format == 'unknown' and office_amenity:
        work_format = 'office'
    structured = infer_format(schedule)
    if work_format == 'unknown' or (structured == 'remote' and office_amenity and work_format == 'office'
            and not re.search(r'работ\w*.{0,20}(?:в офисе|из офиса)|только.{0,15}офис', text)):
        work_format = structured
    lines = [line.strip(' *\t') for line in conditions.splitlines()
             if re.search(r'формат|график|удален|гибрид|офис', normalize(line))]
    label = '; '.join([schedule, *lines]).strip('; ')
    return work_format, label


def parse_publication(row, base_url, categories, experiences, schedules, allow_unknown_category=False):
    if not isinstance(row, dict) or type(row.get('internalId')) is not int or row['internalId'] <= 0:
        raise ParseError('Сбер: отсутствует идентификатор вакансии')
    title, company = text_field(row, 'title'), text_field(row, 'company')
    if not title or not company:
        raise ParseError('Сбер: отсутствует название или компания')
    category_id = row.get('specializationId')
    if category_id is not None and type(category_id) not in (str, int):
        raise ParseError('Сбер: неизвестный идентификатор категории')
    reported_category = text_field(row, 'specialization')
    unknown_category = allow_unknown_category and not reported_category
    category = '' if unknown_category else reference(row, 'specializationId', categories)
    # A stable selected ID is the filter boundary. Display labels can be
    # renamed independently by the dictionary and publication endpoints.
    if not unknown_category and not category:
        raise ParseError('Сбер: неизвестная категория')
    category = reported_category or category
    experience = reference(row, 'experienceId', experiences)
    schedule = reference(row, 'workScheduleId', schedules)
    body = [text_field(row, key) for key in ('introduction', 'duties', 'requirements')]
    if not any(body):
        raise ParseError('Сбер: отсутствует собственное описание вакансии')
    conditions = text_field(row, 'conditions')
    description = '\n'.join(part for part in [*body, conditions] if part)
    lower_bound = re.search(r'^(?:от\s+|более\s+)?(\d+)', normalize(experience))
    level = 'experienced' if lower_bound and int(lower_bound[1]) >= 3 else 'junior' if normalize(experience) == 'без опыта' else 'unknown'
    if level != 'experienced' and re.search(r'стаж[её]р|стажиров|\bintern\b', normalize(title)):
        level = 'intern'
    city, region = text_field(row, 'city'), text_field(row, 'region')
    work_format, label = work_conditions(schedule, conditions)
    identifier = str(row['internalId'])
    return Vacancy('sber', identifier, title, company,
        base_url.rstrip('/') + '/search/' + public_slug(title) + '-' + identifier + '/',
        description=description, categories=[category] if category else [], cities=[city or region] if city or region else [],
        notes=['Категория не подтверждена справочником; направление определено только по названию'] if unknown_category else [],
        experience=experience, level=level, work_format=work_format, work_format_label=label,
        salary=salary_text(row), salary_min=row.get('salary_min'), salary_max=row.get('salary_max'))


def parse_publications(text, base_url, skip, size, categories, experiences, schedules, allow_unknown_category=False):
    data = response_data(text)
    if not isinstance(data, dict) or type(data.get('total')) is not int or data['total'] < 0 or not isinstance(data.get('vacancies'), list):
        raise ParseError('Сбер: некорректные счётчики выдачи')
    total, rows = data['total'], data['vacancies']
    if len(rows) != min(size, max(0, total - skip)):
        raise ParseError('Сбер: получена неполная страница вакансий')
    result = [parse_publication(row, base_url, categories, experiences, schedules, allow_unknown_category) for row in rows]
    if len({v.external_id for v in result}) != len(result):
        raise ParseError('Сбер: повторные идентификаторы внутри страницы')
    return result, total


def collect_sber(settings, client, rules):
    api_url = settings['api_url'].rstrip('/')
    base_url = settings['url']
    if urlsplit(api_url).netloc != urlsplit(base_url).netloc:
        raise ParseError('Сбер: API и карьерный сайт должны иметь один хост')
    size, max_pages = settings.get('page_size', 100), settings.get('max_pages', 50)
    if type(size) is not int or not 1 <= size <= 100 or type(max_pages) is not int or not 1 <= max_pages <= MAX_SOURCE_PAGES:
        raise ValueError(f'Сбер: page_size должен быть от 1 до 100, max_pages — от 1 до {MAX_SOURCE_PAGES}')
    terms = settings.get('search_terms', [])
    if not isinstance(terms, list) or len(terms) > 32 or any(not isinstance(term, str) or not 2 <= len(term.strip()) <= 60 for term in terms):
        raise ValueError('Сбер: search_terms должен содержать до 32 слов длиной 2–60 символов')
    terms = list(dict.fromkeys(normalize(term) for term in terms))
    areas = parse_dictionary(client.get(api_url + '/profAreas'))
    categories = {key: value for key, value in areas.items() if contains(rules['security_patterns'], value)}
    if not categories and not terms:
        raise ParseError('Сбер: в справочнике не найдены категории ИБ')
    experiences = parse_dictionary(client.get(api_url + '/experiences'))
    schedules = parse_dictionary(client.get(api_url + '/schedules'))
    def fetch_all(term=None):
        def fetch(skip):
            query = [('skip', skip), ('take', size)]
            if term is None:
                query += [('profAreas', key) for key in categories]
            else:
                query += [('searchString', term)]
            batch, total = parse_publications(client.get(api_url + '/publications?' + urlencode(query)),
                base_url, skip, size, categories if term is None else areas, experiences, schedules, allow_unknown_category=term is not None)
            # Provider search may expand words. The shared local keyword
            # decision, rather than a provider's search hint, selects notices.
            return batch, total
        # The API has no public stable-sort option. Equal-ranked results may
        # cross page boundaries. Repair only by proving the full unique count;
        # never accept a deduplicated but incomplete catalogue.
        unique_rows, expected = {}, None
        for attempt in range(3):
            rows, total = fetch(0)
            if math.ceil(total / size) > max_pages:
                raise ParseError('Сбер: превышен max_pages; выдача неполна')
            if expected is None:
                expected = total
            if total != expected:
                raise ParseError('Сбер: выдача изменилась во время загрузки; повторим позже')
            batches = [rows]
            for offset in range(size, total, size):
                batch, current_total = fetch(offset)
                if current_total != expected:
                    raise ParseError('Сбер: выдача изменилась во время загрузки; повторим позже')
                batches.append(batch)
            for batch in batches:
                for v in batch:
                    if v.external_id in unique_rows and unique_rows[v.external_id] != v:
                        raise ParseError('Сбер: противоречивые версии вакансии при повторной проверке')
                    unique_rows[v.external_id] = v
            if len(unique_rows) == expected:
                return list(unique_rows.values())
            if len(unique_rows) > expected:
                raise ParseError('Сбер: состав выдачи изменился при неизменном счётчике')
        raise ParseError('Сбер: API повторил карточки; полный уникальный список не подтверждён')

    unique = {v.external_id: v for v in fetch_all()} if categories else {}
    for term in terms:
        for vacancy in fetch_all(term):
            if vacancy.external_id in unique and unique[vacancy.external_id] != vacancy:
                raise ParseError('Сбер: противоречивые версии вакансии между запросами; повторим позже')
            unique[vacancy.external_id] = vacancy
    return list(unique.values())
