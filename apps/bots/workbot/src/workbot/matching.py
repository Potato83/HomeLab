"""Literal keywords for jobs and events; no implicit career filters."""
import re
from datetime import datetime, timezone
from .models import Decision, Vacancy, normalize

def contains(patterns, text):
    return any(re.search(p, normalize(text), re.I) for p in patterns)

def infer_format(text):
    text = normalize(text)
    if re.search(r"не (предусмотрен[ао]?|рассматриваем).{0,15}удален|удаленн.{0,15}(невозмож|не предусмотр|не рассматрива)", text):
        return 'office'
    if re.search(r'гибрид|hybrid', text):
        return 'hybrid'
    if re.search(r'удаленн|дистанционн.{0,10}работ|\bremote\b', text):
        return 'remote'
    if re.search(r'работ.{0,20}(в офисе|из офиса)|офисн.{0,10}(формат|работ)|on.site', text) or text in ('в офисе', 'из офиса', 'офис'):
        return 'office'
    return 'unknown'

def entry_level(title, level):
    if re.search(r'стаж[её]р|стажиров|\bintern(?:ship)?\b|\btrainee\b', normalize(title)):
        return 'intern'
    return level if level in ('intern', 'junior') else 'junior'

def keyword_hit(word, text):
    word = normalize(word)
    # Short acronyms must be words: ИБ must not match ошибка.
    if len(word) <= 3 and word.isalnum():
        return bool(re.search(r'(?<!\w)' + re.escape(word) + r'(?!\w)', text))
    return word in text

def decide(v: Vacancy, rules, allow_unverified=False) -> Decision:
    if v.recruitment_status not in ('open', 'unknown', 'closed'):
        return Decision(False, 'Неизвестный статус набора')
    if not v.active or v.recruitment_status == 'closed' or v.event_status == 'cancelled':
        return Decision(False, 'Набор закрыт или объявление неактуально')
    if v.kind == 'event':
        try:
            start = datetime.fromisoformat(v.start_at.replace('Z', '+00:00'))
            end = datetime.fromisoformat(v.end_at.replace('Z', '+00:00'))
            if start.tzinfo is None or end.tzinfo is None or end < start or end <= datetime.now(timezone.utc):
                return Decision(False, 'Событие завершилось или дата не подтверждена')
        except (ValueError, TypeError):
            return Decision(False, 'Дата события не подтверждена')
    text = normalize(' '.join([v.title, *v.categories, v.description]))
    if any(keyword_hit(w, text) for w in rules.get('exclude_words', [])):
        return Decision(False, 'Исключённое слово')
    hits = [w for w in rules.get('include_words', []) if keyword_hit(w, text)]
    if not hits:
        return Decision(False, 'Нет ключевых слов')
    warnings = ['Набор / регистрация не подтверждены; проверьте у организатора'] if v.recruitment_status == 'unknown' else []
    return Decision(True, ', '.join(hits[:8]), warnings)
