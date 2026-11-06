import os
from pathlib import Path
import re
import math
import tomllib
from urllib.parse import urlsplit
from .limits import DEFAULT_SCAN_TIMEOUT_SECONDS, MAX_SOURCE_PAGES


def validate_url(value, field):
    if not isinstance(value, str):
        raise ValueError(f"{field}: требуется HTTPS-адрес")
    parts = urlsplit(value)
    # Accessing .port validates its syntax and range without making a request.
    _ = parts.port
    if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password or any(c.isspace() for c in value):
        raise ValueError(f"{field}: требуется корректный HTTPS-адрес без данных входа")


def load_env(path: str = ".env"):
    if not Path(path).exists():
        return
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or not re.fullmatch(r"[A-Z_][A-Z_0-9]*", key.strip()):
            raise ValueError("Некорректная строка в .env")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def load_config(path: str):
    with open(path, "rb") as f:
        config = tomllib.load(f)
    return validate_config(config)


def validate_config(config, require_enabled=True):
    monitor = config["monitor"]
    from .directions import LABELS
    filters = config['filters']
    filters.setdefault('min_salary', 0)
    filters.setdefault('employment_types', ['any'])
    if type(filters['min_salary']) is not int or not 0 <= filters['min_salary'] <= 10000000:
        raise ValueError('min_salary должен быть целым числом от 0 до 10000000')
    from .employment import LABELS as EMPLOYMENT_LABELS
    employment = filters['employment_types']
    if (not isinstance(employment, list) or not employment
            or any(not isinstance(value, str) or value not in (*EMPLOYMENT_LABELS, 'any') for value in employment)
            or ('any' in employment and len(employment) != 1)):
        raise ValueError('employment_types: any либо допустимые типы занятости через запятую')
    values = filters.get('directions', ['security'])
    if not isinstance(values, list) or not values or any(not isinstance(v, str) or v not in LABELS for v in values):
        raise ValueError('directions: выберите security, devsecops, devops, mlops')
    for field in ('events_enabled', 'event_unverified', 'event_cancellations', 'event_updates'):
        if type(filters.get(field, False if field == 'events_enabled' else True)) is not bool:
            raise ValueError(f'{field} должен быть true или false')
    from .event_details import LANGUAGES, PRICES
    languages = filters.get('event_languages', ['any'])
    if not isinstance(languages, list) or not languages or any(not isinstance(v, str) or v not in LANGUAGES for v in languages) or ('any' in languages and len(languages) != 1):
        raise ValueError('event_languages: any либо ru,en,other,unknown через запятую')
    if not isinstance(filters.get('event_price', 'any'), str) or filters.get('event_price', 'any') not in PRICES:
        raise ValueError('event_price: any, free, free_or_unknown или paid')
    if filters.get('event_audience', 'any') not in ('any', 'beginner', 'beginner_or_unknown'):
        raise ValueError('event_audience: any, beginner или beginner_or_unknown')
    days = filters.get('event_days', 90)
    if type(days) is not int or not 1 <= days <= 365:
        raise ValueError('event_days должен быть целым числом от 1 до 365')
    alerts = config.setdefault("alerts", {})
    if not isinstance(monitor, dict) or not isinstance(alerts, dict):
        raise ValueError("monitor и alerts должны быть секциями конфигурации")
    monitor.setdefault('scan_timeout_seconds', DEFAULT_SCAN_TIMEOUT_SECONDS)
    alerts.setdefault("enabled", True)
    alerts.setdefault("failure_threshold", 3)
    alerts.setdefault("min_failure_seconds", 3600)
    if type(alerts["enabled"]) is not bool:
        raise ValueError("alerts.enabled должен быть true или false")
    if type(alerts["failure_threshold"]) is not int or alerts["failure_threshold"] < 1:
        raise ValueError("alerts.failure_threshold должен быть положительным целым числом")
    if type(alerts["min_failure_seconds"]) is not int or alerts["min_failure_seconds"] < 0:
        raise ValueError("alerts.min_failure_seconds должен быть неотрицательным целым числом")
    for key in ("interval_seconds", "max_messages_per_cycle"):
        if type(monitor[key]) is not int or monitor[key] <= 0:
            raise ValueError(f"{key} должен быть положительным целым числом")
    for key in ("request_timeout_seconds", "request_delay_seconds"):
        value = monitor[key]
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError(f"{key} должен быть конечным неотрицательным числом")
    if monitor["interval_seconds"] < 60 or monitor["request_timeout_seconds"] == 0:
        raise ValueError("Интервал должен быть от 60 секунд, таймаут — больше нуля")
    if type(monitor['scan_timeout_seconds']) is not int or not 30 <= monitor['scan_timeout_seconds'] <= 3600:
        raise ValueError('scan_timeout_seconds должен быть целым числом от 30 до 3600')
    from .schedule import parse as parse_schedule
    parse_schedule(monitor.get('schedule', 'off'))
    for key in ("security_patterns", "junior_patterns", "senior_title_patterns", "city_patterns"):
        if not isinstance(config['filters'][key], list) or any(not isinstance(p, str) for p in config['filters'][key]):
            raise ValueError(f"{key} должен быть списком регулярных выражений")
        for pattern in config["filters"][key]:
            try:
                re.compile(pattern, re.I)
            except re.error:
                raise ValueError(f"Некорректное регулярное выражение в {key}") from None
    for field, choices in [('allowed_formats', {'remote', 'office', 'hybrid'}), ('allowed_levels', {'intern', 'junior'})]:
        if field in config['filters']:
            value = config['filters'][field]
            if not isinstance(value, list) or not value or any(not isinstance(item, str) or item not in choices for item in value):
                raise ValueError(f'Некорректные значения {field}')
    for field, choices in [('event_formats', {'remote', 'office', 'hybrid'}), ('event_types', {'ctf', 'meetup', 'hackathon', 'conference', 'webinar', 'unknown'})]:
        values = filters.get(field, list(choices))
        if not isinstance(values, list) or not values or any(not isinstance(v, str) or v not in choices for v in values):
            raise ValueError(f'Некорректные значения {field}')
    for field in ('include_words', 'exclude_words'):
        value = config['filters'].get(field, [])
        if not isinstance(value, list) or len(value) > 32 or any(not isinstance(x, str) or not 1 <= len(x) <= 60 for x in value):
            raise ValueError(f'Некорректные значения {field}')
    if type(config['filters'].get('include_unknown_location', True)) is not bool or type(monitor.get('paused', False)) is not bool:
        raise ValueError('Флаги паузы и неизвестного расположения должны быть true или false')
    if monitor.get('delivery_mode', 'instant') not in ('instant', 'digest') or type(monitor.get('digest_seconds', 3600)) is not int or monitor.get('digest_seconds', 3600) < 60:
        raise ValueError('Некорректный режим или интервал подборки')
    known = {"yandex", "kaspersky", "bastion", "croc", "croc_internships", "mts", "x5_internships", "pt_start_training", "pt_fast_track", "sber", "hh", "habr", "yandex_jobs", 'ctftime', 'ods_events'}
    if set(config["sources"]) - known:
        raise ValueError("Неизвестный источник в config.toml")
    for name, settings in config['sources'].items():
        if not isinstance(settings, dict) or type(settings.get('enabled', False)) is not bool:
            raise ValueError(f"{name}.enabled должен быть true или false")
        if not settings.get('enabled'):
            continue
        if 'interval_seconds' in settings and (type(settings['interval_seconds']) is not int or settings['interval_seconds'] < 60):
            raise ValueError(f'{name}.interval_seconds должен быть целым числом от 60')
        validate_url(settings.get('url'), f'{name}.url')
        if name == 'ctftime' and settings['url'] != 'https://ctftime.org/api/v1/events/':
            raise ValueError('ctftime.url должен вести к публичному API календаря')
        if name == 'ods_events':
            if settings.get('api_url', 'https://api.ods.ai/api/events/list/') != 'https://api.ods.ai/api/events/list/':
                raise ValueError('ods_events.api_url должен вести к публичному API ODS')
            pages = settings.get('max_pages', 30)
            if type(pages) is not int or not 1 <= pages <= MAX_SOURCE_PAGES:
                raise ValueError(f'ods_events.max_pages должен быть от 1 до {MAX_SOURCE_PAGES}')
        if type(settings.get('notify_unverified', False)) is not bool:
            raise ValueError(f'{name}.notify_unverified должен быть true или false')
        if name in ('kaspersky', 'mts', 'sber', 'hh', 'habr', 'yandex_jobs'):
            pages = settings.get('max_pages', 50 if name in ('mts', 'sber') else 30)
            if type(pages) is not int or not 1 <= pages <= MAX_SOURCE_PAGES:
                raise ValueError(f'{name}.max_pages должен быть от 1 до {MAX_SOURCE_PAGES}')
        if name == 'croc':
            validate_url(settings.get('catalog_url'), 'croc.catalog_url')
        if name == 'yandex_jobs':
            validate_url(settings.get('api_url'), 'yandex_jobs.api_url')
            parts = urlsplit(settings['api_url'])
            if parts.netloc != 'yandex.ru' or parts.path != '/jobs/api/jobs/publications/':
                raise ValueError('yandex_jobs.api_url должен вести к публичному каталогу Яндекса')
        if name in ('mts', 'sber', 'hh'):
            validate_url(settings.get('api_url'), f'{name}.api_url')
            if name == 'hh':
                if settings['api_url'].rstrip('/') != 'https://api.hh.ru':
                    raise ValueError('hh.api_url должен быть https://api.hh.ru')
                if type(settings.get('lookback_days', 365)) is not int or not 30 <= settings.get('lookback_days', 365) <= 1095:
                    raise ValueError('hh.lookback_days должен быть целым числом от 30 до 1095')
            elif urlsplit(settings['api_url']).netloc != urlsplit(settings['url']).netloc:
                raise ValueError(f'{name}.api_url и {name}.url должны иметь один хост')
            size = settings.get('page_size', 100)
            if type(size) is not int or not 1 <= size <= 100:
                raise ValueError(f'{name}.page_size должен быть целым числом от 1 до 100')
        if name in ('mts', 'sber', 'hh'):
            minimum = 1 if name == 'mts' else 0
            terms = settings.get('search_terms', [] if name in ('sber', 'hh') else None)
            if not isinstance(terms, list) or not minimum <= len(terms) <= 32 or any(not isinstance(t, str) or not 2 <= len(t.strip()) <= 60 for t in terms):
                raise ValueError(f'{name}.search_terms: требуется от {minimum} до 32 слов длиной 2–60 символов')
    if require_enabled and not any(s.get("enabled") for s in config["sources"].values()):
        raise ValueError("Нужно включить хотя бы один источник")
    return config
