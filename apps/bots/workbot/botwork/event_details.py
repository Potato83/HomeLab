"""Conservative extraction of explicit event terms from the event's own text."""
import re
from .models import normalize, Vacancy

LANGUAGES = {'any', 'ru', 'en', 'other', 'unknown'}
PRICES = {'any', 'free', 'free_or_unknown', 'paid'}


def cancelled_title(title):
    value = normalize(title)
    return bool(re.search(r'^(?:\[(?:cancelled|canceled|отменено)\]|(?:cancelled|canceled|отменено)\s*[:\-])|[\[(](?:cancelled|canceled|отменено)[\])]$', value))


def last_known_cancellation(previous, title=None):
    v = Vacancy(**previous.to_dict())
    v.event_status, v.active, v.event_check_error = 'cancelled', True, ''
    if isinstance(title, str) and title.strip():
        v.title = title
    note = 'Отмена подтверждена; даты и условия показаны по последнему сохранённому объявлению'
    v.notes = [note, *[n for n in v.notes if n != note][:2]]
    return v


def enrich_terms(v):
    text = v.description.replace('\\r\\n', '\n').replace('\\n', '\n')
    languages, prices = [], []
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for i, line in enumerate(lines):
        # HTML can put a label and its value in adjacent text nodes.
        if line.endswith(':') and i + 1 < len(lines):
            line += ' ' + lines[i + 1]
        lang = re.fullmatch(r'(?:язык(?:\s+(?:мероприятия|докладов|общения))?|languages?|event language|working language)\s*[:—-]\s*(.{1,100})', line, re.I)
        price = re.fullmatch(r'(?:стоимость(?:\s+участия)?|цена(?:\s+билета)?|registration fee|participation fee|ticket price|entry fee|cost)\s*[:—-]\s*(.{1,100})', line, re.I)
        if lang:
            languages.append(lang.group(1))
        if price:
            prices.append(price.group(1))
        if re.fullmatch(r'участие бесплатно[.!]?|participation is free[.!]?', line, re.I):
            prices.append('бесплатно')
    languages, prices = list(dict.fromkeys(languages)), list(dict.fromkeys(prices))
    codes = set()
    for value in languages:
        value = normalize(value)
        found = set()
        if re.search(r'русск|russian|\bru(?:-ru)?\b', value):
            found.add('ru')
        if re.search(r'англий|english|\ben(?:-(?:us|gb))?\b', value):
            found.add('en')
        if re.search(r'немецк|german|француз|french|испанск|spanish|китайск|chinese|японск|japanese|\b(?:de|fr|es|zh|ja)\b', value):
            found.add('other')
        if found:
            codes.update(found)
    v.language_codes = sorted(codes)
    v.language = ', '.join(languages)[:100] if codes else 'не указан'
    kinds = set()
    for value in prices:
        normalized = normalize(value).strip('.! ')
        money = re.findall(r'[$€₽]\s*([0-9][0-9 .,]*)|([0-9][0-9 .,]*)\s*(?:₽|руб|rub|usd|eur|€|\$)', normalized)
        amounts = [a or b for a, b in money]
        positive = any(re.search('[1-9]', amount) for amount in amounts)
        zero = any(not re.search('[1-9]', amount) for amount in amounts)
        if re.fullmatch(r'бесплатно|бесплатное участие|free(?: of charge)?|[$€₽]?\s*0(?:[.,]0{1,2})?\s*(?:₽|руб\.?|рублей|rub|eur|usd|€|\$)?', normalized):
            kinds.add('free')
        elif not re.search(r'бесплат|\bfree\b|[?]', normalized) and ((positive and not zero) or re.fullmatch(r'платно|платное участие|paid', normalized)):
            kinds.add('paid')
        else:
            kinds.add('unknown')
    v.price = '; '.join(prices)[:100] if prices else 'не указана'
    v.price_kind = next(iter(kinds)) if len(kinds) == 1 else 'unknown'
    return v


def terms_decision(v, rules):
    languages = rules.get('event_languages', ['any'])
    codes = set(v.language_codes) or {'unknown'}
    if 'any' not in languages and not codes.intersection(languages):
        return 'Язык мероприятия исключён настройками'
    price = rules.get('event_price', 'any')
    if price == 'free_or_unknown' and v.price_kind not in ('free', 'unknown'):
        return 'Платное участие исключено настройками'
    if price in ('free', 'paid') and v.price_kind != price:
        return 'Стоимость участия не соответствует настройкам'
    return ''
