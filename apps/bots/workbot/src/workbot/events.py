"""Public event calendars with explicit dates and conservative registration status."""
from datetime import datetime, timedelta, timezone
import json
import re
from urllib.parse import urlencode
from urllib.parse import urlsplit
from .matching import decide as decide_event
from .http import FetchError
from .event_details import enrich_terms, cancelled_title, last_known_cancellation
from .config import validate_url
from .models import Vacancy, normalize
from .sources import ParseError, soup
from .limits import MAX_SOURCE_PAGES

EVENT_SOURCES = {'ctftime', 'ods_events'}


def instant(value):
    if not isinstance(value, str):
        raise ValueError('Дата отсутствует')
    date = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if date.tzinfo is None:
        raise ValueError('У даты отсутствует часовой пояс')
    return date


def event_now():
    return datetime.now(timezone.utc)


def safe_link(url):
    if not isinstance(url, str) or len(url) > 800:
        return ''
    try:
        validate_url(url, 'Ссылка мероприятия')
    except ValueError:
        return ''
    return url


def parse_ctftime(text):
    try:
        rows = json.loads(text)
        if not isinstance(rows, list):
            raise ValueError
        result = []
        for row in rows:
            identifier, title = row['id'], row['title']
            if type(identifier) is not int or identifier <= 0 or not isinstance(title, str) or not title.strip() or type(row['onsite']) is not bool:
                raise ValueError
            start, end = instant(row['start']), instant(row['finish'])
            if end < start or row['ctftime_url'] != f'https://ctftime.org/event/{identifier}/':
                raise ValueError
            organizers = [x['name'] for x in row['organizers']]
            if any(not isinstance(x, str) for x in organizers) or any(not isinstance(row.get(k, ''), str) for k in ('description', 'location', 'restrictions', 'format', 'url')):
                raise ValueError
            notes = ['Условия участия: ' + row['restrictions']] if row.get('restrictions') else []
            if row.get('format'):
                notes.append('Формат CTF: ' + row['format'])
            result.append(enrich_terms(Vacancy('ctftime', str(identifier), title, ', '.join(organizers) or 'CTFtime', row['ctftime_url'],
                kind='event', description=row.get('description', ''), categories=['Информационная безопасность'],
                cities=[row['location']] if row.get('location') and row['onsite'] else [],
                work_format='office' if row['onsite'] else 'remote', recruitment_status='unknown',
                start_at=start.isoformat(), end_at=end.isoformat(), event_type='ctf', notes=notes,
                event_status='cancelled' if cancelled_title(title) else 'scheduled',
                application_url=safe_link(row.get('url', '')))))
        if len({v.external_id for v in result}) != len(result):
            raise ValueError
        return result
    except (ValueError, KeyError, TypeError):
        raise ParseError('CTFtime: изменились карточки, даты или ссылки календаря') from None


def collect_ctftime(settings, client, rules):
    if settings['url'] != 'https://ctftime.org/api/v1/events/':
        raise ParseError('CTFtime: неизвестный адрес API')
    start = event_now()
    finish = start + timedelta(days=rules.get('event_days', 90))
    unique, calls = {}, 0
    def window(a, b):
        nonlocal calls
        calls += 1
        if calls > 100:
            raise ParseError('CTFtime: превышен лимит запросов календаря')
        rows = parse_ctftime(client.get(settings['url'] + '?' + urlencode({'limit': 100, 'start': int(a.timestamp()), 'finish': int(b.timestamp())})))
        if len(rows) >= 100:
            if b - a <= timedelta(hours=1):
                raise ParseError('CTFtime: выдача может быть обрезана')
            mid = a + (b - a) / 2
            window(a, mid); window(mid, b)
            return
        for v in rows:
            if v.external_id in unique and unique[v.external_id] != v:
                raise ParseError('CTFtime: событие изменилось между запросами')
            unique[v.external_id] = v
    window(start, finish)
    for data in settings.get('_watched_events', []):
        previous = Vacancy(**data)
        if previous.external_id in unique:
            continue
        if previous.source != 'ctftime' or not re.fullmatch(r'[1-9][0-9]*', previous.external_id):
            raise ParseError('CTFtime: неверный идентификатор отслеживаемого события')
        try:
            raw = json.loads(client.get(settings['url'] + previous.external_id + '/'))
            try:
                current = parse_ctftime(json.dumps([raw]))[0]
            except ParseError:
                if isinstance(raw, dict) and type(raw.get('id')) is int and str(raw['id']) == previous.external_id and raw.get('ctftime_url') == 'https://ctftime.org/event/' + previous.external_id + '/' and isinstance(raw.get('title'), str) and cancelled_title(raw['title']):
                    current = last_known_cancellation(previous, raw['title'])
                else:
                    raise
            if current.external_id != previous.external_id:
                raise ParseError('CTFtime: карточка другого события')
        except (FetchError, ParseError, ValueError, KeyError, TypeError):
            current = previous
            current.event_check_error = 'Карточка недоступна или изменилась; отмена не подтверждена'
        unique[current.external_id] = current
    return list(unique.values())


def ods_page(text, offset):
    try:
        data = json.loads(text)
        rows, total, size = data['events'], data['total'], data['limit']
        if type(total) is not int or total < 0 or type(size) is not int or size != 20 or type(data['offset']) is not int or data['offset'] != offset or not isinstance(rows, list) or len(rows) != min(size, max(0, total-offset)):
            raise ValueError
        return rows, total
    except (ValueError, KeyError, TypeError):
        raise ParseError('ODS: неполная страница календаря или неизвестные счётчики') from None


def ods_card(row):
    try:
        identifier, slug, title = row['id'], row['slug'], row['title']
        if not isinstance(identifier, str) or not re.fullmatch(r'[a-f0-9-]{36}', identifier) or not isinstance(slug, str) or not re.fullmatch(r'[a-zA-Z0-9_-]+', slug) or not isinstance(title, str) or not title.strip():
            raise ValueError
        start, end = instant(row['start_dt']), instant(row['end_dt'])
        if end < start or row['status'] not in ('active', 'announced', 'finished', 'cancelled', 'completed'):
            raise ValueError
        place, description = row['location'] or '', row['description'] or ''
        categories = [h['title'] for h in row['related_hubs']]
        if any(not isinstance(x, str) for x in [place, description, *categories]):
            raise ValueError
        fmt = 'office' if re.search(r'offline|офлайн', normalize(place)) else 'remote' if re.search(r'online|онлайн', normalize(place)) else 'unknown'
        own = normalize(title + ' ' + description)
        event_type = next((key for key, p in [('ctf', r'\bctf\b'), ('meetup', 'meetup|митап'), ('hackathon', 'хакатон|hackathon'), ('conference', 'конференц|conference'), ('webinar', 'вебинар|webinar')] if re.search(p, own)), 'unknown')
        return enrich_terms(Vacancy('ods_events', identifier, title, 'Open Data Science', 'https://ods.ai/events/' + slug,
            kind='event', description=description, categories=categories,
            cities=[place] if place and fmt != 'remote' else [], work_format=fmt, recruitment_status='unknown',
            start_at=start.isoformat(), end_at=end.isoformat(), event_type=event_type,
            event_status='cancelled' if row['status'] == 'cancelled' else 'scheduled',
            active=row['status'] not in ('finished', 'completed')))
    except (ValueError, KeyError, TypeError):
        raise ParseError('ODS: неизвестная карточка или даты мероприятия') from None


def collect_ods(settings, client, rules):
    api = settings.get('api_url', 'https://api.ods.ai/api/events/list/')
    if api != 'https://api.ods.ai/api/events/list/':
        raise ParseError('ODS: неизвестный адрес API')
    max_pages = settings.get('max_pages', 30)
    if type(max_pages) is not int or not 1 <= max_pages <= MAX_SOURCE_PAGES:
        raise ValueError(f'ODS: max_pages должен быть от 1 до {MAX_SOURCE_PAGES}')
    rows, expected, seen = [], None, set()
    for page in range(max_pages):
        batch, total = ods_page(client.get(api + '?' + urlencode({'is_active': 'true', 'sort': '-start_dt', 'limit': 20, 'offset': page * 20})), page * 20)
        if expected is None:
            expected = total
        if total != expected:
            raise ParseError('ODS: календарь изменился во время загрузки')
        for raw in batch:
            v = ods_card(raw)
            if v.external_id in seen:
                raise ParseError('ODS: календарь повторил карточки')
            seen.add(v.external_id); rows.append(v)
        if len(rows) == total:
            break
    else:
        raise ParseError('ODS: превышен max_pages; календарь неполон')
    watched = {v['external_id']: Vacancy(**v) for v in settings.get('_watched_events', [])}
    candidates = rules | {'event_unverified': True, 'event_languages': ['any'], 'event_price': 'any'}
    result = []
    for v in rows:
        if v.external_id in watched or decide_event(v, candidates).matched:
            try:
                v = ods_detail(v, client, listed=True)
            except (FetchError, ParseError):
                if v.external_id not in watched:
                    raise
                v.event_check_error = 'Карточка недоступна или изменилась; условия не перепроверены'
        result.append(v)
    for identifier, previous in watched.items():
        if identifier in seen:
            continue
        if previous.source != 'ods_events' or not re.fullmatch(r'https://ods\.ai/events/[a-zA-Z0-9_-]+', previous.url):
            raise ParseError('ODS: неверный адрес отслеживаемого события')
        try:
            v = ods_detail(previous, client, listed=False)
        except (FetchError, ParseError):
            v = previous
            v.event_check_error = 'Карточка недоступна или изменилась; отмена не подтверждена'
        result.append(v)
    return result


def ods_detail(previous, client, listed=True):
    try:
        data = soup(client.get(previous.url)).select_one('#__NEXT_DATA__')
        event = json.loads(data.get_text())['props']['pageProps']['event']
        try:
            v = ods_card(event)
        except ParseError:
            if not listed and isinstance(event, dict) and event.get('id') == previous.external_id and event.get('status') == 'cancelled':
                return last_known_cancellation(previous, event.get('title'))
            raise
        if v.external_id != previous.external_id or v.url != previous.url:
            raise ValueError
        if listed and (v.title != previous.title or instant(v.start_at) != instant(previous.start_at) or instant(v.end_at) != instant(previous.end_at)):
            raise ValueError
        content = '\n'.join(block['content'] for block in event.get('content', []) if block.get('type') == 'html')
        body = soup(content)
        v.description += '\n' + body.get_text('\n', strip=True)
        registration = event.get('registration')
        if registration is not None:
            if not isinstance(registration, dict) or type(registration.get('is_completed')) is not bool or type(registration.get('is_limit_exceed')) is not bool:
                raise ValueError
            if registration['is_limit_exceed']:
                v.recruitment_status = 'closed'
            v.application_url = v.url
        own_text = normalize(v.description)
        if re.search(r'регистрация закрыта|registration (?:is )?closed', own_text):
            v.recruitment_status = 'closed'
        elif v.recruitment_status != 'closed' and re.search(r'регистрация открыта|registration (?:is )?open', own_text):
            v.recruitment_status = 'open'
        for a in body.select('a[href]'):
            if re.search('зарегистр|регистрац|register', normalize(a.get_text(' ', strip=True))) and safe_link(a['href']):
                v.application_url = a['href']
                v.recruitment_status = 'unknown'
                v.notes.append('Требуется внешняя регистрация; форма организатора не проверена')
                break
        if registration and registration.get('deadline'):
            v.registration_deadline = instant(registration['deadline']).isoformat()
        return enrich_terms(v)
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ParseError('ODS: изменилась карточка или статус регистрации') from None
