"""Optional weekly scan windows in Moscow time."""
from datetime import datetime, timedelta, timezone
import re

DAYS = ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')
MOSCOW = timezone(timedelta(hours=3))


def parse(value):
    if value == 'off':
        return None
    if not isinstance(value, str):
        raise ValueError('Расписание: off или mon,tue 09:00-18:00 (МСК).')
    parts = value.split(';')
    if len(parts) > 14:
        raise ValueError('Допустимо до 14 промежутков расписания.')
    windows = []
    for part in parts:
        match = re.fullmatch(r'([a-z,]+) (\d{2}):(\d{2})-(\d{2}):(\d{2})', part.strip())
        if not match:
            raise ValueError('Расписание: off или mon,tue 09:00-18:00; fri 22:00-03:00 (МСК).')
        days = match[1].split(',')
        h1, m1, h2, m2 = map(int, match.groups()[1:])
        start, end = h1 * 60 + m1, h2 * 60 + m2
        if len(set(days)) != len(days) or any(day not in DAYS for day in days) or h1 > 23 or h2 > 23 or m1 > 59 or m2 > 59 or start == end:
            raise ValueError('Укажите дни mon–sun и разные корректные часы начала и конца.')
        windows.append((days, start, end))
    return windows


def allows_scan(value, now=None):
    windows = parse(value)
    if windows is None:
        return True
    current = (now or datetime.now(timezone.utc)).astimezone(MOSCOW)
    minute = current.hour * 60 + current.minute
    today = DAYS[current.weekday()]
    yesterday = DAYS[(current.weekday() - 1) % 7]
    return any((today in days and start <= minute < end) if start < end else
               (today in days and minute >= start) or (yesterday in days and minute < end)
               for days, start, end in windows)
