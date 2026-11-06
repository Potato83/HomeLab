"""Conservative mapping of source-specific employment labels."""

from .models import normalize

LABELS = {
    'full_time': 'полная занятость',
    'part_time': 'частичная занятость',
    'project': 'проектная работа',
    'internship': 'стажировка',
    'temporary': 'временная работа',
    'volunteer': 'волонтёрство',
}


def employment_types(values):
    if not values:
        return [], False
    result = set()
    for value in values:
        text = normalize(value)
        if text in ('full', 'full-time') or 'полная занятость' in text or 'full time' in text:
            result.add('full_time')
        elif text in ('part', 'part-time') or 'частичная занятость' in text or 'part time' in text:
            result.add('part_time')
        elif text in ('project', 'project-based') or 'проектная работа' in text or 'contract' in text:
            result.add('project')
        elif text in ('probation', 'internship', 'intern') or 'стажиров' in text or 'internship' in text:
            result.add('internship')
        elif 'temporary' in text or 'временная работа' in text:
            result.add('temporary')
        elif text == 'volunteer' or 'волонтер' in text or 'волонтёр' in text:
            result.add('volunteer')
    return sorted(result), bool(result)
