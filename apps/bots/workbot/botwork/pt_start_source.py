"""The paid-internship programme on PT Start's training page."""

import re

from .matching import contains
from .models import Vacancy, normalize
from .program_sources import recruitment_state
from .sources import ParseError, soup


def record_text(record):
    return record.get_text(' ', strip=True)


def is_security_direction(name, description, rules):
    heading = normalize(name)
    if contains(rules['security_patterns'], name):
        return True
    # Working on a security product does not make ordinary development/QA an IB role.
    if re.match(r'разработк|тестирован|devops|mlops|маркетинг', heading):
        return False
    return bool(re.search(r'кибер|антивирус|вредонос|уязвим|криптограф|информационн.{0,5} безопасност|\b(иб|впо|soc|siem|edr|osint|ciso)\b',
                          normalize(name + ' ' + description)))


def parse_pt_training(html, url, rules):
    doc = soup(html)
    records = doc.select('div.t-rec')
    if not records:
        raise ParseError('PT Start: отсутствуют разделы программы')
    positions = {id(record): index for index, record in enumerate(records)}
    heroes = []
    for atom in doc.select('.tn-atom'):
        if normalize(atom.get_text(' ', strip=True)) == 'стажировка с обучением':
            parent = atom.find_parent('div', class_='t-rec')
            if parent is not None and id(parent) not in {id(x) for x in heroes}:
                heroes.append(parent)
    if len(heroes) != 1:
        raise ParseError('PT Start: не удалось определить первый экран стажировки с обучением')
    anchors = {}
    for name in ('steps', 'speciality'):
        anchor = doc.select_one(f'a[name="{name}"]')
        parent = anchor.find_parent('div', class_='t-rec') if anchor else None
        if parent is None:
            raise ParseError('PT Start: отсутствуют разделы этапов или направлений стажировки')
        anchors[name] = positions[id(parent)]
    if not positions[id(heroes[0])] < anchors['steps'] < anchors['speciality']:
        raise ParseError('PT Start: изменился порядок разделов программы')
    applications = []
    for record in records[anchors['speciality'] + 1:]:
        atoms = [normalize(atom.get_text(' ', strip=True)) for atom in record.select('.tn-atom')]
        if 'подать заявку' in atoms and 'сделай первый шаг' in normalize(record_text(record)):
            applications.append(record)
    if len(applications) != 1:
        raise ParseError('PT Start: не удалось определить актуальный блок подачи заявки')
    application = applications[0]
    end = positions[id(application)]
    states = [recruitment_state(record_text(record)) for record in (heroes[0], application)]
    known = [state for state in states if state is not None]
    if not known or len(set(known)) != 1:
        raise ParseError('PT Start: статус набора неизвестен или противоречив')
    stage_atoms = [normalize(atom.get_text(' ', strip=True))
                   for record in records[anchors['steps']:anchors['speciality']]
                   for atom in record.select('.tn-atom')]
    if 'офер на оплачиваемую стажировку' not in stage_atoms:
        raise ParseError('PT Start: не подтверждён этап оплачиваемой стажировки; страница может быть образовательной')
    headings = []
    for index in range(anchors['speciality'] + 1, end):
        groups = records[index].find_all('div', class_=lambda name: name and re.fullmatch(r'content\d+', name))
        for group in groups:
            name = ' '.join(atom.get_text(' ', strip=True) for atom in group.select('.tn-atom') if atom.get_text(strip=True))
            if not name:
                raise ParseError('PT Start: отсутствует название направления')
            headings.append((index, name))
    if not headings:
        raise ParseError('PT Start: не найдены направления стажировки')
    indices = {index for index, _ in headings}
    selected = {}
    for index, name in headings:
        body = None
        for next_index in range(index + 1, end):
            if next_index in indices:
                break
            candidate = record_text(records[next_index])
            if candidate:
                body = candidate
                break
        if not body or not normalize(body).startswith('чем занимаемся'):
            raise ParseError('PT Start: не найдено собственное описание направления')
        if is_security_direction(name, body, rules):
            key = normalize(name)
            if key in selected and selected[key][1] != normalize(body):
                raise ParseError('PT Start: противоречивые копии направления')
            selected[key] = (name, normalize(body))
    if not selected:
        raise ParseError('PT Start: не подтверждены направления информационной безопасности')
    directions = [value[0] for value in selected.values()]
    names = '; '.join(directions)
    return [Vacancy('pt_start_training', 'with-education',
        'Стажировка с обучением в информационной безопасности — PT Start',
        'Positive Technologies', url, categories=['Информационная безопасность'],
        description='Программа с обучением и отбором на оплачиваемую стажировку. Направления: ' + names,
        level='intern', active=known[0], notes=[
            'Предусмотрены обучение и отбор на оплачиваемую стажировку.',
            'Направления ИБ: ' + names])]
