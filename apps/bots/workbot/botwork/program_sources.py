"""Internship programmes with explicitly checked recruitment state."""

import hashlib
import re
from urllib.parse import urljoin, urlsplit

from .matching import contains
from .models import Vacancy, normalize
from .sources import ParseError, soup


def recruitment_state(text):
    text = normalize(text)
    closed = bool(re.search(r"набор(?: на стажировку| заявок)? (?:закрыт|завершен)|прием заявок завершен", text))
    opened = bool(re.search(r"набор(?: на стажировку| заявок)? открыт|прием заявок открыт", text))
    if closed and opened:
        raise ParseError("Противоречивые статусы набора")
    return False if closed else True if opened else None


def parse_x5_internships(html, url, rules):
    doc = soup(html)
    title = doc.select_one('main h1')
    registration = doc.select_one('main section#registration')
    if not title or "стажиров" not in normalize(title.get_text()) or registration is None:
        raise ParseError("X5: не найдена страница регистрации на стажировку")
    hero = title.find_parent('section')
    # Read only the current application area and hero, never historical FAQ text.
    states = [recruitment_state(registration.get_text(" ", strip=True))]
    if hero:
        states.append(recruitment_state(hero.get_text(" ", strip=True)))
    known = [state for state in states if state is not None]
    if not known or len(set(known)) != 1:
        raise ParseError("X5: статус набора неизвестен или противоречив")
    active = known[0]
    rows = {}
    for card in doc.select('main article.rl-card'):
        heading = card.select_one('h3.rl-title')
        if not heading:
            raise ParseError("X5: отсутствует название направления")
        name = heading.get_text(" ", strip=True)
        if not contains(rules['security_patterns'], name):
            continue
        link = card.select_one('a[href]')
        if not link:
            raise ParseError("X5: отсутствует ссылка направления")
        target = urljoin(url, link['href'])
        parts = urlsplit(target)
        if parts.scheme != 'https' or parts.netloc != urlsplit(url).netloc or parts.path.rstrip('/') != '/internships' or parts.fragment != 'registration':
            raise ParseError("X5: неизвестная ссылка подачи заявки")
        description = card.select_one('p.rl-text')
        identifier = hashlib.sha256(normalize(name).encode()).hexdigest()[:16]
        vacancy = Vacancy('x5_internships', identifier, 'Стажировка: ' + name, 'X5', target,
            description=description.get_text(" ", strip=True) if description else '',
            categories=[name], level='intern', active=active)
        if identifier in rows and rows[identifier].description != vacancy.description:
            raise ParseError("X5: противоречивые копии направления")
        rows[identifier] = vacancy
    if not rows:
        raise ParseError("X5: не найдены направления ИБ; возможно, изменилась страница")
    return list(rows.values())
