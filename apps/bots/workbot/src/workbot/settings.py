"""Owner-controlled, persistent search settings over a read-only TOML baseline."""
from copy import deepcopy
from html import escape
import json
import re
import sqlite3

from .config import validate_config
from .hh_source import valid_token
from .models import normalize

KEY = 'search_settings'
USAGE = ('/set city Санкт-Петербург | Москва\n/set formats remote,hybrid,office\n'
         '/set directions security,devsecops,devops,mlops\n/set events on|off\n/set event_unverified on|off\n/set event_days 90\n'
         '/set event_formats remote,office,hybrid\n/set event_types ctf,meetup,hackathon,conference,webinar,unknown\n'
         '/set event_languages any (либо ru,en,other,unknown)\n/set event_price any|free|free_or_unknown|paid\n/set event_cancellations on|off\n'
         '/set event_updates on|off\n'
         '/set event_audience any|beginner|beginner_or_unknown\n'
         '/set levels intern,junior\n/set min_salary 100000|off (руб./мес.)\n'
         '/set employment any|full_time,part_time,project,internship,temporary,volunteer\n'
         '/set unknown on|off\n/set include SOC | AppSec\n'
         '/set exclude продажи | поддержка\n/set interval 60 (минуты)\n/set schedule mon,tue 09:00-18:00; fri 22:00-03:00 (МСК)\n/set schedule off\n/set pause on|off\n/set delivery instant|digest\n/set digest 60 (минуты)\n'
         '/source NAME on|off\n/source NAME interval 120\n/source hh lookback 365\n'
         '/source pt_fast_track unverified on|off\n'
         '/profile save NAME|use NAME|list|delete NAME — профили фильтров\n'
         '/settings_reset — вернуть TOML. Для очистки списка: /set include -')


def saved_config(base, path):
    from contextlib import closing
    from pathlib import Path
    file = Path(path)
    if not file.exists():
        return deepcopy(base)
    try:
        with closing(sqlite3.connect(file.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            rows = dict(db.execute('SELECT key, value FROM state WHERE key IN (?, ?, ?)', (KEY, 'search_profiles', 'active_search_profile')))
        class State:
            def get_state(self, key, default=None):
                return rows.get(key, default)
        return effective_config(base, State())
    except (sqlite3.Error, OSError):
        raise ValueError('Не удалось прочитать сохранённые настройки из WORKBOT_DB.') from None


def effective_config(base, store):
    result = deepcopy(base)
    try:
        overrides = json.loads(store.get_state(KEY, '{}'))
        if not isinstance(overrides, dict) or set(overrides) - {'filters', 'monitor', 'sources'}:
            raise ValueError
        for section, values in overrides.items():
            if not isinstance(values, dict):
                raise ValueError
            if section == 'sources':
                for name, settings in values.items():
                    if name not in result['sources']:
                        continue
                    allowed = {'enabled', 'interval_seconds', 'notify_unverified'} | ({'lookback_days'} if name == 'hh' else set())
                    if not isinstance(settings, dict) or set(settings) - allowed:
                        raise ValueError
                    result['sources'][name].update(settings)
            else:
                result[section].update(values)
        filters = result['filters']
        validated = validate_config(result, require_enabled=False)
        raw_profiles = json.loads(store.get_state('search_profiles', '{}'))
        active_profile = store.get_state('active_search_profile', '')
        if not isinstance(raw_profiles, dict) or len(raw_profiles) > 10:
            raise ValueError
        profiles = []
        for name, value in raw_profiles.items():
            if not isinstance(name, str) or not isinstance(value, dict) or set(value) != {'filters'} or not isinstance(value['filters'], dict):
                raise ValueError
            candidate = deepcopy(validated)
            candidate['filters'] = value['filters']
            candidate = validate_config(candidate, require_enabled=False)
            profiles.append({'name': name, 'filters': candidate['filters']})
        profiles.sort(key=lambda profile: (profile['name'] != active_profile, profile['name'].casefold()))
        if not any(profile['filters'].get('events_enabled', False) for profile in profiles) and not filters.get('events_enabled', False):
            for name in ('ctftime', 'ods_events'):
                if name in result['sources']:
                    result['sources'][name]['enabled'] = False
            validated = validate_config(result, require_enabled=False)
        if profiles:
            validated['_search_profiles'] = profiles
        return validated
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ValueError('Сохранённые настройки повреждены. Восстановите базу или используйте /settings_reset.') from None


def settings_text(config):
    f, m = config['filters'], config['monitor']
    enabled = [name for name, value in config['sources'].items() if value.get('enabled')]
    from .directions import LABELS, selected
    from .employment import LABELS as EMPLOYMENT_LABELS
    lines = ['<b>Настройки поиска</b>', 'Направления:\n' + '\n'.join('• ' + LABELS[k] for k in selected(f)),
        'Мероприятия: ' + ('включены' if f.get('events_enabled', False) else 'отключены'),
        f'Горизонт мероприятий: {f.get("event_days", 90)} дн.',
        'Форматы мероприятий: ' + ', '.join(f.get('event_formats', ['remote', 'office', 'hybrid'])),
        'Типы мероприятий: ' + ', '.join(f.get('event_types', ['ctf', 'meetup', 'hackathon', 'conference', 'webinar', 'unknown'])),
        'Языки мероприятий: ' + ', '.join(f.get('event_languages', ['any'])),
        'Стоимость мероприятий: ' + {'any': 'любая / неизвестная', 'free': 'только подтверждённо бесплатно', 'free_or_unknown': 'бесплатно или нужно уточнить', 'paid': 'только платные'}[f.get('event_price', 'any')],
        'Аудитория мероприятий: ' + {'any': 'любая', 'beginner': 'только явно для начинающих', 'beginner_or_unknown': 'для начинающих или уровень не указан'}[f.get('event_audience', 'any')],
        'Отмена / возобновление ранее присланных: ' + ('сообщать' if f.get('event_cancellations', True) else 'не сообщать'),
        'Изменение условий мероприятий: ' + ('сообщать' if f.get('event_updates', True) else 'не сообщать'),
        'Регистрация не подтверждена: ' + ('присылать с пометкой' if f.get('event_unverified', True) else 'исключать'),
        'Города:\n' + '\n'.join('• ' + escape(city.strip()) for city in f.get('city_label', 'Санкт-Петербург').split('|')),
        'Форматы: ' + ', '.join(f.get('allowed_formats', ['remote', 'office', 'hybrid'])),
        'Уровни: ' + ', '.join(f.get('allowed_levels', ['intern', 'junior'])),
        'Минимальная зарплата: ' + (f'{f.get("min_salary", 0):,} ₽/мес.'.replace(',', ' ') if f.get('min_salary', 0) else 'без фильтра'),
        'Тип занятости: ' + ', '.join('любой' if v == 'any' else EMPLOYMENT_LABELS[v] for v in f.get('employment_types', ['any'])),
        'Неизвестные город/формат: ' + ('присылать с пометкой' if f.get('include_unknown_location', True) else 'исключать'),
        'Ключевые слова:\n' + ('\n'.join('• ' + escape(x) for x in f.get('include_words', [])) or 'без дополнительного ограничения'),
        'Исключения:\n' + ('\n'.join('• ' + escape(x) for x in f.get('exclude_words', [])) or 'нет'),
        f'Интервал по умолчанию: {m["interval_seconds"] // 60} мин.',
        'Расписание проверок (МСК): ' + escape(m.get('schedule', 'off')),
        'Мониторинг: ' + ('на паузе' if m.get('paused') else 'работает'),
        'Уведомления: ' + ('сразу' if m.get('delivery_mode', 'instant') == 'instant' else f'подборка раз в {m.get("digest_seconds", 3600) // 60} мин.'),
        'Включены: ' + escape(', '.join(enabled) or 'нет')]
    for name, source in config['sources'].items():
        if source.get('interval_seconds'):
            lines.append(escape(name) + f': {source["interval_seconds"] // 60} мин.')
        if name == 'hh':
            lines.append(f'HH: поиск за последние {source.get("lookback_days", 365)} дн. при разбиении больших выдач')
        if 'notify_unverified' in source:
            lines.append(escape(name) + ': непроверенный набор ' + ('присылать' if source['notify_unverified'] else 'исключать'))
    return '\n'.join(lines) + '\n\n' + escape(USAGE)


class Settings:
    def __init__(self, base, store):
        self.base, self.store = base, store

    def execute(self, command, arguments, update_id):
        if type(update_id) is not int or update_id < 0:
            raise ValueError('Отправьте команду ещё раз.')
        db = self.store.db
        with db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT response FROM settings_commands WHERE update_id=?', (update_id,)).fetchone()
            if old:
                return old['response']
            try:
                patch = {} if command == '/settings_reset' else json.loads(self.store.get_state(KEY, '{}'))
                if command != '/settings_reset':
                    self.change(patch, command, arguments)
                class Candidate:
                    def get_state(self, key, default=None):
                        return json.dumps(patch) if key == KEY else default
                candidate = effective_config(self.base, Candidate())
                try:
                    previous = effective_config(self.base, self.store)
                except ValueError:
                    previous = None
                if command == '/settings_reset':
                    db.execute("INSERT INTO state(key,value) VALUES ('active_search_profile','') ON CONFLICT(key) DO UPDATE SET value='' ")
                elif command == '/set':
                    from .profiles import sync_active_profile
                    sync_active_profile(self.store, candidate['filters'])
                db.execute('INSERT INTO state(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                    (KEY, json.dumps(patch, ensure_ascii=False)))
                if previous is None or any(candidate['monitor'].get(key) != previous['monitor'].get(key) for key in ('delivery_mode', 'digest_seconds')):
                    db.execute("INSERT INTO state(key,value) VALUES ('digest_next_at','') ON CONFLICT(key) DO UPDATE SET value='' ")
                response = 'Настройки сохранены. Применяются без перезапуска.'
            except (ValueError, TypeError, KeyError, AttributeError) as exc:
                response = str(exc) or 'Некорректное значение настройки.'
            db.execute('INSERT INTO settings_commands VALUES (?,?)', (update_id, response))
        return response

    def change(self, patch, command, arguments):
        parts = arguments.split(maxsplit=1)
        if len(parts) != 2:
            raise ValueError(USAGE)
        key, value = parts[0].lower(), parts[1].strip()
        def boolean(text):
            if text not in ('on', 'off'):
                raise ValueError('Используйте on или off.')
            return text == 'on'
        def minutes(text):
            if not text.isdigit() or not 1 <= int(text) <= 10080:
                raise ValueError('Интервал: целое число минут от 1 до 10080.')
            return int(text) * 60
        if command == '/source':
            if key not in self.base['sources']:
                raise ValueError('Неизвестный источник. Список: /sources')
            source = patch.setdefault('sources', {}).setdefault(key, {})
            if value in ('on', 'off'):
                if value == 'on' and key == 'hh':
                    import os
                    if not valid_token(os.environ.get('HH_API_TOKEN', '')):
                        raise ValueError('Для hh.ru сначала задайте HH_API_TOKEN в .env.')
                source['enabled'] = boolean(value)
            elif value.startswith('interval '):
                source['interval_seconds'] = minutes(value.split(maxsplit=1)[1])
            elif value.startswith('unverified ') and 'notify_unverified' in self.base['sources'][key]:
                source['notify_unverified'] = boolean(value.split(maxsplit=1)[1])
            elif value.startswith('lookback ') and key == 'hh':
                days = value.split(maxsplit=1)[1]
                if not days.isdigit() or not 30 <= int(days) <= 1095:
                    raise ValueError('HH: глубина разбиения — от 30 до 1095 дней.')
                source['lookback_days'] = int(days)
            else:
                raise ValueError(USAGE)
            return
        if command != '/set':
            raise ValueError('Неизвестная команда настроек.')
        f, m = patch.setdefault('filters', {}), patch.setdefault('monitor', {})
        if key == 'directions':
            from .directions import LABELS
            values = list(dict.fromkeys(v.strip().lower() for v in value.split(',')))
            if not values or any(v not in LABELS for v in values):
                raise ValueError('Направления через запятую: security, devsecops, devops, mlops')
            f['directions'] = values
        elif key in ('events', 'event_unverified', 'event_cancellations', 'event_updates'):
            f['events_enabled' if key == 'events' else key] = boolean(value)
        elif key == 'event_languages':
            from .event_details import LANGUAGES
            values = list(dict.fromkeys(v.strip().lower() for v in value.split(',')))
            if not values or any(v not in LANGUAGES for v in values) or ('any' in values and len(values) != 1):
                raise ValueError('Языки: any либо список ru,en,other,unknown через запятую.')
            f[key] = values
        elif key == 'event_price':
            from .event_details import PRICES
            if value not in PRICES:
                raise ValueError('Стоимость: any, free, free_or_unknown или paid.')
            f[key] = value
        elif key == 'event_audience':
            if value not in ('any', 'beginner', 'beginner_or_unknown'):
                raise ValueError('Аудитория мероприятий: any, beginner или beginner_or_unknown.')
            f[key] = value
        elif key == 'event_days':
            if not value.isdigit() or not 1 <= int(value) <= 365:
                raise ValueError('Горизонт: целое число дней от 1 до 365.')
            f['event_days'] = int(value)
        elif key == 'city':
            cities = [x.strip() for x in value.split('|') if x.strip()]
            if not 1 <= len(cities) <= 20 or any(len(x) > 80 for x in cities):
                raise ValueError('Укажите от 1 до 20 городов через |.')
            patterns = []
            for city in cities:
                normalized = normalize(city)
                patterns.append(r'санкт[ -]петербург|\bспб\b|saint[ -]petersburg|st[ .-]*petersburg' if normalized in ('спб', 'санкт-петербург', 'санкт петербург') else re.escape(normalized))
            f.update(city_patterns=patterns, city_label=' | '.join(cities))
        elif key in ('formats', 'levels', 'event_formats', 'event_types'):
            choices = {'ctf', 'meetup', 'hackathon', 'conference', 'webinar', 'unknown'} if key == 'event_types' else {'remote', 'office', 'hybrid'} if key in ('formats', 'event_formats') else {'intern', 'junior'}
            values = list(dict.fromkeys(v.strip().lower() for v in value.split(',')))
            if not values or any(v not in choices for v in values):
                raise ValueError('Допустимые значения через запятую: ' + ', '.join(sorted(choices)))
            f[key if key.startswith('event_') else 'allowed_' + key] = values
        elif key == 'min_salary':
            if value.lower() == 'off':
                f[key] = 0
            elif value.isdigit() and 1 <= int(value) <= 10000000:
                f[key] = int(value)
            else:
                raise ValueError('Минимальная зарплата: целое число рублей от 1 до 10000000 или off.')
        elif key == 'employment':
            from .employment import LABELS
            values = ['any'] if value.lower() == 'any' else list(dict.fromkeys(v.strip().lower() for v in value.split(',')))
            if not values or any(v not in LABELS for v in values) or ('any' in values and len(values) != 1):
                raise ValueError('Тип занятости: any либо full_time, part_time, project, internship, temporary, volunteer через запятую.')
            f['employment_types'] = values
        elif key in ('include', 'exclude'):
            words = [] if value == '-' else list(dict.fromkeys(x.strip() for x in value.split('|') if x.strip()))
            if len(words) > 32 or any(len(word) > 60 for word in words):
                raise ValueError('До 32 слов или фраз длиной до 60 символов, через |.')
            f[key + '_words'] = words
        elif key == 'unknown':
            f['include_unknown_location'] = boolean(value)
        elif key == 'interval':
            m['interval_seconds'] = minutes(value)
        elif key == 'schedule':
            from .schedule import parse
            value = value.lower()
            parse(value)
            m['schedule'] = value
        elif key == 'pause':
            m['paused'] = boolean(value)
        elif key == 'delivery':
            if value not in ('instant', 'digest'):
                raise ValueError('Используйте instant или digest.')
            m['delivery_mode'] = value
        elif key == 'digest':
            m['digest_seconds'] = minutes(value)
        else:
            raise ValueError(USAGE)


def handle_settings(telegram, monitor, update):
    command = telegram.command(update)
    if command not in ('/settings', '/set', '/source', '/settings_reset'):
        return False
    if command == '/settings':
        if telegram.arguments(update) != 'all':
            from .interface import settings_panel
            text, keyboard = settings_panel(monitor)
            telegram.send(text, reply_markup=keyboard)
            return True
        from .sites import length
        chunk = ''
        for line in settings_text(monitor.config).splitlines():
            if chunk and length(chunk + '\n' + line) > 3500:
                telegram.send(chunk)
                chunk = ''
            chunk += ('\n' if chunk else '') + line
        if chunk:
            telegram.send(chunk)
        return True
    try:
        response = Settings(monitor.base_config, monitor.store).execute(command, telegram.arguments(update), update.get('update_id'))
        monitor.reload_settings()
    except ValueError as exc:
        response = str(exc)
    except sqlite3.Error:
        response = 'Не удалось сохранить настройки. Повторите команду позже.'
    telegram.send(escape(response))
    return True
