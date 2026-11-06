"""PT Fast Track directions, with explicit unverified-intake handling."""

from dataclasses import replace
import hashlib
import re
from urllib.parse import urljoin, urlsplit

from .http import FetchError
from .matching import contains
from .models import Vacancy, normalize
from .program_sources import recruitment_state
from .pt_start_source import record_text
from .sources import ParseError, soup

FORM_HOST = 'form-pt-start.ptsecurity.com'


def fast_format(label):
    text = normalize(label)
    clauses = re.split(r'[.;\n]', text)
    remote = False
    for clause in clauses:
        has_remote = bool(re.search(r'удален(?:к|н)|\bremote\b', clause))
        negative = bool(re.search(r'\bне\b|невозмож|только.*офис', clause))
        hybrid = bool(re.search(r'гибрид|hybrid', clause))
        if has_remote and not negative and (not hybrid or re.search(r'\bили\b', clause)):
            remote = True
    if remote:
        work_format = 'remote'
    elif 'гибрид' in text or 'hybrid' in text:
        work_format = 'hybrid'
    elif 'офис' in text or 'on-site' in text:
        work_format = 'office'
    else:
        work_format = 'unknown'
    cities = []
    for location in re.findall(r'\(([^)]+)\)', label):
        for part in re.split(r'[,;/]|\s+и\s+', location):
            part = part.strip()
            if part and not re.search(r'другие города|час|недел|\d', normalize(part)):
                cities.append(part)
    if not cities:
        for pattern, city in [(r'санкт[ -]петербург|\bспб\b', 'Санкт-Петербург'), (r'москв', 'Москва')]:
            if re.search(pattern, text):
                cities.append(city)
    return work_format, list(dict.fromkeys(cities))


def parse_fast_track(html, url, rules):
    doc = soup(html)
    records = doc.select('div.t-rec')
    positions = {id(record): index for index, record in enumerate(records)}
    heroes = {id(parent): parent for atom in doc.select('.tn-atom')
              if normalize(atom.get_text(' ', strip=True)) == 'стажировка fast track'
              if (parent := atom.find_parent('div', class_='t-rec')) is not None}
    if len(heroes) != 1:
        raise ParseError('Fast Track: не найден первый экран программы')
    hero = next(iter(heroes.values()))
    anchors = {}
    for name in ('steps', 'speciality'):
        anchor = doc.select_one(f'a[name="{name}"]')
        parent = anchor.find_parent('div', class_='t-rec') if anchor else None
        if parent is None:
            raise ParseError('Fast Track: не найдены разделы этапов и направлений')
        anchors[name] = positions[id(parent)]
    if not positions[id(hero)] < anchors['steps'] < anchors['speciality']:
        raise ParseError('Fast Track: изменился порядок разделов')
    stage_atoms = [normalize(atom.get_text(' ', strip=True)) for record in records[anchors['steps']:anchors['speciality']]
                   for atom in record.select('.tn-atom')]
    if 'получи офер на стажировку' not in stage_atoms:
        raise ParseError('Fast Track: не подтверждён этап стажировки')
    applications = [record for record in records[anchors['speciality'] + 1:]
        if 'сделай первый шаг' in normalize(record_text(record))
        and any(normalize(atom.get_text(' ', strip=True)) == 'подать заявку' for atom in record.select('.tn-atom'))]
    if len(applications) != 1:
        raise ParseError('Fast Track: не найден блок заявки')
    application = applications[0]
    end = positions[id(application)]
    states = [recruitment_state(record_text(record)) for record in (hero, application)]
    known = [state for state in states if state is not None]
    if len(set(known)) > 1:
        raise ParseError('Fast Track: противоречивый статус набора')
    status = ('open' if known[0] else 'closed') if known else 'unknown'
    links = {urljoin(url, anchor['href']) for record in (hero, application) for anchor in record.select('a[href]')
             if normalize(anchor.get_text(' ', strip=True)) == 'подать заявку'}
    if len(links) != 1:
        raise ParseError('Fast Track: ссылка формы отсутствует или неоднозначна')
    form_url = next(iter(links))
    parts = urlsplit(form_url)
    if parts.scheme != 'https' or parts.netloc != FORM_HOST or not re.fullmatch(r'/internships/\d{4}/?', parts.path):
        raise ParseError('Fast Track: неизвестный адрес формы')
    headings = []
    for index in range(anchors['speciality'] + 1, end):
        for group in records[index].find_all('div', class_=lambda name: name and re.fullmatch(r'content\d+', name)):
            name = ' '.join(atom.get_text(' ', strip=True) for atom in group.select('.tn-atom') if atom.get_text(strip=True))
            headings.append((index, name))
    indices = {index for index, _ in headings}
    result = {}
    incomplete = []
    for index, name in headings:
        # Only explicit IB direction names; ordinary development/QA is excluded.
        if not contains(rules['security_patterns'], name):
            continue
        if not records[index].get('id'):
            raise ParseError('Fast Track: у направления ИБ отсутствует якорь')
        body = None
        for next_index in range(index + 1, end):
            if next_index in indices:
                break
            if record_text(records[next_index]):
                body = records[next_index]
                break
        if body is None:
            incomplete.append(name)
            continue
        if not normalize(record_text(body)).startswith('нам важны'):
            raise ParseError('Fast Track: отсутствует собственное описание направления ИБ')
        atoms = [atom.get_text(' ', strip=True) for atom in body.select('.tn-atom') if atom.get_text(strip=True)]
        labels = [atoms[i + 1] for i, value in enumerate(atoms[:-1]) if normalize(value) == 'формат работы'
                  and not normalize(atoms[i + 1]).startswith('в каких командах')]
        if len(set(normalize(label) for label in labels)) > 1:
            raise ParseError('Fast Track: противоречивые форматы направления')
        label = labels[0] if labels else ''
        work_format, cities = fast_format(label)
        identifier = hashlib.sha256(normalize(name).encode()).hexdigest()[:16]
        v = Vacancy('pt_fast_track', identifier, 'Стажировка Fast Track: ' + name,
            'Positive Technologies', url + '#' + records[index]['id'], description=record_text(body),
            categories=['Информационная безопасность'], level='intern',
            cities=cities, work_format=work_format, work_format_label=label,
            active=status != 'closed', recruitment_status=status, application_url=form_url)
        if identifier in result and result[identifier].description != v.description:
            raise ParseError('Fast Track: противоречивые копии направления')
        result[identifier] = v
    if not result:
        raise ParseError('Fast Track: не найдены направления ИБ')
    if incomplete:
        note = 'На сайте нет собственного описания; пропущены направления: ' + '; '.join(incomplete)
        return [replace(v, notes=[*v.notes, note]) for v in result.values()]
    return list(result.values())


def parse_fast_form(html):
    doc = soup(html)
    main = doc.select_one('main') or doc.body or doc
    headings = ' '.join(node.get_text(' ', strip=True) for node in main.select('h1, h2'))
    forms = main.select('form')
    text = normalize(headings + ' ' + ' '.join(form.get_text(' ', strip=True) for form in forms))
    if 'fast track' not in text or 'стажиров' not in text:
        return None, 'Форма не подтверждает приём именно на стажировку Fast Track'
    state = recruitment_state(text)
    if re.search(r'заявки (?:больше )?не принимаются|регистрация закрыта|прием заявок закрыт', text):
        if state is True:
            raise ParseError('Fast Track: противоречивые статусы формы')
        state = False
    if state is False:
        return False, 'Форма Fast Track сообщает о закрытом наборе'
    def enabled(node):
        return not node.has_attr('disabled') and node.get('aria-disabled') != 'true' and node.find_parent('fieldset', disabled=True) is None
    for form in forms:
        fields = [node for node in form.select('input:not([type="hidden"])') if enabled(node)]
        buttons = [node for node in form.select('button[type="submit"], input[type="submit"]') if enabled(node)]
        form_text = normalize(form.get_text(' ', strip=True) + ' ' + ' '.join(node.get('value', '') for node in buttons))
        own_text = form_text + ' ' + normalize(headings)
        application = bool(re.search(r'отправить.{0,20}(заявк|анкет)|подать заявку|заявка на стажировку', form_text))
        if application and 'fast track' in own_text and any(node.get('type') == 'email' for node in fields) and len(fields) >= 2 and buttons:
            return True, 'Доступна форма приёма заявок на стажировку Fast Track'
    return None, 'Форму нельзя подтвердить как действующую: недостаточно доступных полей или статуса'


def collect_fast_track(settings, client, rules):
    rows = parse_fast_track(client.get(settings['url']), settings['url'], rules)
    page_status = rows[0].recruitment_status
    if page_status == 'closed':
        return rows
    form_state, note = None, ''
    try:
        form_state, note = parse_fast_form(client.get(rows[0].application_url))
    except FetchError as exc:
        note = 'Форму заявки не удалось проверить: ' + str(exc)
    if page_status == 'open' and form_state is False:
        raise ParseError('Fast Track: страница и форма противоречат друг другу')
    status = 'closed' if form_state is False else 'open' if page_status == 'open' or form_state is True else 'unknown'
    return [replace(v, recruitment_status=status, active=status != 'closed', notes=[*v.notes, *([note] if note else [])]) for v in rows]
