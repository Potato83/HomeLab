import math
import os
from pathlib import Path
import re
import tomllib
from urllib.parse import urlsplit
from .limits import MAX_SOURCE_PAGES

SOURCES = {'yandex', 'kaspersky', 'bastion', 'croc', 'croc_internships', 'mts',
           'x5_internships', 'pt_start_training', 'pt_fast_track', 'sber', 'hh',
           'habr', 'yandex_jobs', 'ctftime', 'ods_events'}
DEFAULT_WORDS = ['информационная безопасност', 'информационной безопасност',
                 'кибербезопас', 'security', 'SOC', 'SIEM', 'AppSec',
                 'DevSecOps', 'DevOps', 'SRE', 'MLOps', 'CTF', 'пентест']

def validate_url(value, field):
    if not isinstance(value, str):
        raise ValueError(f'{field}: требуется HTTPS-адрес')
    parts = urlsplit(value)
    _ = parts.port
    if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password or any(c.isspace() for c in value):
        raise ValueError(f'{field}: требуется HTTPS-адрес без данных входа')

def words(value, *, allow_empty=False):
    if (not isinstance(value, list) or len(value) > 32 or (not value and not allow_empty)
            or any(not isinstance(w, str) or not 1 <= len(w.strip()) <= 60 for w in value)):
        raise ValueError('Укажите от 1 до 32 слов / фраз, до 60 символов каждое')
    return list(dict.fromkeys(w.strip() for w in value))

def load_env(path='.env'):
    if not Path(path).exists():
        return
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, sep, value = line.partition('=')
        if not sep or not re.fullmatch(r'[A-Z_][A-Z_0-9]*', key.strip()):
            raise ValueError('Некорректная строка в .env')
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))

def load_config(path):
    with open(path, 'rb') as f:
        return validate_config(tomllib.load(f))

def validate_config(config, require_enabled=True):
    monitor = config.setdefault('monitor', {})
    if type(monitor.setdefault('verify_ru_tls', True)) is not bool:
        raise ValueError('verify_ru_tls: требуется true/false')
    defaults = dict(interval_seconds=3600, request_timeout_seconds=25,
                    request_delay_seconds=0.5, scan_timeout_seconds=600,
                    max_messages_per_cycle=10)
    for key, default in defaults.items():
        value = monitor.setdefault(key, default)
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError(f'{key}: требуется конечное число')
    for key, lower, upper in [('interval_seconds', 60, 86400), ('scan_timeout_seconds', 30, 3600), ('max_messages_per_cycle', 1, 20)]:
        if type(monitor[key]) is not int or not lower <= monitor[key] <= upper:
            raise ValueError(f'{key}: требуется целое число от {lower} до {upper}')
    if not 1 <= monitor['request_timeout_seconds'] <= 120 or not 0 <= monitor['request_delay_seconds'] <= 60:
        raise ValueError('Таймаут: 1–120 секунд, задержка: 0–60 секунд')
    # Accept old TOML during rollout, but do not re-enable the retired profiles.
    old = config.get('filters', {})
    keyword = config.setdefault('keywords', {})
    include = words(keyword.get('include', old.get('include_words') or DEFAULT_WORDS))
    exclude = words(keyword.get('exclude', old.get('exclude_words', [])), allow_empty=True)
    config['keywords'] = {'include': include, 'exclude': exclude}
    config['filters'] = {'include_words': include, 'exclude_words': exclude,
                         'events_enabled': True, 'event_days': 90,
                         'security_patterns': [re.escape(w.casefold()) for w in include],
                         'senior_title_patterns': []}
    sources = config.get('sources', {})
    if not isinstance(sources, dict) or set(sources) - SOURCES:
        raise ValueError('Неизвестный источник')
    for name, settings in sources.items():
        if not isinstance(settings, dict) or type(settings.get('enabled', False)) is not bool:
            raise ValueError(f'{name}.enabled: требуется true/false')
        if not settings.get('enabled'):
            continue
        validate_url(settings.get('url'), f'{name}.url')
        for field in ('api_url', 'catalog_url'):
            if field in settings:
                validate_url(settings[field], f'{name}.{field}')
        pages = settings.get('max_pages', 30)
        if type(pages) is not int or not 1 <= pages <= MAX_SOURCE_PAGES:
            raise ValueError(f'{name}.max_pages: от 1 до {MAX_SOURCE_PAGES}')
        size = settings.get('page_size', 100)
        if type(size) is not int or not 1 <= size <= 100:
            raise ValueError(f'{name}.page_size: от 1 до 100')
        interval = settings.get('interval_seconds', monitor['interval_seconds'])
        if type(interval) is not int or not 60 <= interval <= 86400:
            raise ValueError(f'{name}.interval_seconds: от 60 до 86400')
        if name == 'hh' and settings.get('api_url', '').rstrip('/') != 'https://api.hh.ru':
            raise ValueError('hh.api_url должен быть https://api.hh.ru')
        if name in ('mts', 'sber') and urlsplit(settings.get('api_url', '')).netloc != urlsplit(settings['url']).netloc:
            raise ValueError(f'{name}: API и каталог должны иметь один хост')
    if require_enabled and not any(s.get('enabled') for s in sources.values()):
        raise ValueError('Включите хотя бы один источник')
    return config
