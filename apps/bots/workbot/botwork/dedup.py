"""Conservative cross-source matching for duplicate vacancy and event notices."""

from datetime import timezone

from .models import normalize


def vacancy_key(vacancy):
    """Return a strict key; incomplete metadata is not enough to deduplicate."""
    if vacancy.get('kind', 'vacancy') != 'vacancy':
        return None
    title = normalize(vacancy.get('title', ''))
    company = normalize(vacancy.get('company', ''))
    cities = tuple(sorted(normalize(city) for city in vacancy.get('cities', []) if normalize(city)))
    work_format = vacancy.get('work_format', 'unknown')
    if not title or not company or not cities or work_format == 'unknown':
        return None
    return title, company, cities, work_format


def event_key(event):
    """Match only events with the same title, type, and exact time interval."""
    if event.get('kind') != 'event':
        return None
    title = normalize(event.get('title', ''))
    event_type = event.get('event_type', 'unknown')
    if not title or event_type == 'unknown':
        return None
    try:
        start = _utc_instant(event.get('start_at', ''))
        end = _utc_instant(event.get('end_at', ''))
    except (TypeError, ValueError):
        return None
    if end <= start:
        return None
    return 'event', title, event_type, start, end


def _utc_instant(value):
    from datetime import datetime
    if not isinstance(value, str):
        raise ValueError
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError
    return parsed.astimezone(timezone.utc).isoformat()


def notification_key(vacancy):
    """Return a strict cross-source key for either supported record kind."""
    return event_key(vacancy) if vacancy.get('kind') == 'event' else vacancy_key(vacancy)
