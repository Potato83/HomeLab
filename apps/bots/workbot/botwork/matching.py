import re
from .models import Decision, Vacancy, normalize


def contains(patterns, text):
    return any(re.search(p, normalize(text), re.I) for p in patterns)


def infer_format(text):
    text = normalize(text)
    if re.search(r"не (предусмотрен[ао]?|рассматриваем).{0,15}удален|удаленн.{0,15}(невозмож|не предусмотр|не рассматрива)", text):
        return "office"
    if re.search(r"гибрид|hybrid", text):
        return "hybrid"
    if re.search(r"удаленн|дистанционн.{0,10}работ|\bremote\b", text):
        return "remote"
    if re.search(r"работ.{0,20}(в офисе|из офиса)|офисн.{0,10}(формат|работ)|on.site", text) or text in ("в офисе", "из офиса", "офис"):
        return "office"
    return "unknown"


def entry_level(title, level):
    """Classify an already confirmed starting-level vacancy for filters/display."""
    if re.search(r'стаж[её]р|стажиров|\bintern(?:ship)?\b|\btrainee\b', normalize(title)):
        return 'intern'
    return level if level in ('intern', 'junior') else 'junior'


def decide(v: Vacancy, rules, allow_unverified=False) -> Decision:
    if v.kind == 'event':
        from .events import decide_event
        return decide_event(v, rules)
    warnings = []
    if not v.active or v.recruitment_status == 'closed':
        return Decision(False, "Набор закрыт")
    if v.recruitment_status not in ('open', 'closed', 'unknown'):
        return Decision(False, 'Неизвестный статус набора')
    if v.recruitment_status == 'unknown' and not allow_unverified:
        return Decision(False, 'Набор не подтверждён')
    minimum_salary = rules.get('min_salary', 0)
    if minimum_salary:
        currency = normalize(v.salary_currency)
        if currency in ('rub', 'rur', '₽') and v.salary_period == 'monthly' and (v.salary_min is not None or v.salary_max is not None):
            if v.salary_max is not None and v.salary_max < minimum_salary:
                return Decision(False, f'Зарплата ниже порога {minimum_salary:,} ₽'.replace(',', ' '))
            if v.salary_min is None or v.salary_min < minimum_salary:
                warnings.append(f'Диапазон зарплаты пересекает порог {minimum_salary:,} ₽ — уточните сумму'.replace(',', ' '))
        else:
            warnings.append('Сумма, рублёвая валюта или месячный период зарплаты не подтверждены — проверьте вручную')
    allowed_employment = rules.get('employment_types', ['any'])
    if 'any' not in allowed_employment:
        if v.employment_known:
            if not set(v.employment_types).intersection(allowed_employment):
                return Decision(False, 'Тип занятости исключён настройками')
        else:
            warnings.append('Тип занятости не указан — проверьте условия вручную')
    if v.work_format == 'field':
        return Decision(False, 'Разъездная работа не соответствует условиям поиска')
    if v.work_format != 'unknown' and v.work_format not in rules.get('allowed_formats', ['remote', 'office', 'hybrid']):
        return Decision(False, 'Формат работы исключён настройками')
    haystack = normalize(' '.join([v.title, *v.categories, v.description]))
    if any(normalize(word) in haystack for word in rules.get('exclude_words', [])):
        return Decision(False, 'Найдено исключённое ключевое слово')
    if rules.get('include_words') and not any(normalize(word) in haystack for word in rules['include_words']):
        return Decision(False, 'Нет выбранных ключевых слов')
    # Company boilerplate and FAQ must never be passed as the vacancy description.
    from .directions import matches
    # A shared catalogue heading cannot establish which of its distinct roles
    # a vacancy belongs to. Sber combines Data Science and Data Engineering.
    categories = [c for c in v.categories if not ('data scien' in normalize(c) and 'data engineer' in normalize(c))]
    directions = matches(' '.join([v.title, *categories]), rules)
    if not directions:
        return Decision(False, "Нет выбранного направления в названии или категории")
    if contains(rules["senior_title_patterns"], v.title):
        return Decision(False, "Уровень выше junior")
    if v.level == "experienced":
        return Decision(False, "В карточке требуется опыт от 3 лет")
    if v.level in ('middle', 'senior', 'lead'):
        return Decision(False, 'Уровень выше junior')
    if v.level not in ("intern", "junior") and not contains(rules["junior_patterns"], v.title + " " + v.description):
        return Decision(False, "Не подтверждён уровень стажировки или junior")
    level = entry_level(v.title, v.level)
    if level not in rules.get('allowed_levels', ['intern', 'junior']):
        return Decision(False, 'Уровень исключён настройками')
    if v.recruitment_status == 'unknown':
        warnings.append('Набор не подтверждён — проверьте форму заявки вручную')
    if v.work_format == "remote" and re.search(r"командиров|выезд", normalize(v.work_format_label)):
        warnings.append("Удалёнка с командировками или выездами — нужно уточнить условия")
    city_known = bool(v.cities)
    spb = contains(rules["city_patterns"], " ".join(v.cities))
    if v.work_format != "remote":
        if city_known and not spb:
            if v.work_format in ("office", "hybrid"):
                return Decision(False, "Офис или гибрид вне выбранных городов")
            warnings.append("Город вне выбранных городов, формат не указан — нужно уточнить возможность удалёнки")
        elif not city_known:
            warnings.append("Город не указан — нужно уточнить")
        if v.work_format == "unknown":
            warnings.append("Формат работы не указан — нужно уточнить")
        if (not city_known or v.work_format == 'unknown') and not rules.get("include_unknown_location", True):
            return Decision(False, "Не хватает данных о расположении")
    return Decision(True, ' / '.join(directions) + ' · стажировка/junior', warnings)
