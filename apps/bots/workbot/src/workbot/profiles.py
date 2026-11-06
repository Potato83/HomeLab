"""Saved search-filter profiles for quick changes of direction and geography."""

import json
import re
import sqlite3
from html import escape

from .settings import KEY, effective_config

PROFILES_KEY = 'search_profiles'
ACTIVE_KEY = 'active_search_profile'
MAX_PROFILES = 10


def _name(value):
    name = value.strip()
    if not 1 <= len(name) <= 32 or not re.fullmatch(r'[\w -]+', name, re.UNICODE):
        raise ValueError('Название профиля: 1–32 буквы, цифры, пробел, дефис или подчёркивание.')
    return name


def _load(store):
    try:
        profiles = json.loads(store.get_state(PROFILES_KEY, '{}'))
    except (TypeError, ValueError):
        raise ValueError('Список профилей повреждён; проверьте базу данных.') from None
    if not isinstance(profiles, dict) or len(profiles) > MAX_PROFILES:
        raise ValueError('Список профилей повреждён; проверьте базу данных.')
    return profiles


def _write_state(db, key, value):
    db.execute('''INSERT INTO state(key,value) VALUES (?,?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value''', (key, value))


def _save(db, profiles):
    _write_state(db, PROFILES_KEY, json.dumps(profiles, ensure_ascii=False))


def sync_active_profile(store, filters):
    active = store.get_state(ACTIVE_KEY, '')
    if not active:
        return
    profiles = _load(store)
    if active in profiles:
        profiles[active] = {'filters': filters}
        _save(store.db, profiles)
    else:
        _write_state(store.db, ACTIVE_KEY, '')


def _response(store, arguments, update_id, monitor):
    parts = arguments.split(maxsplit=1)
    action = parts[0].lower() if parts else 'list'
    value = parts[1].strip() if len(parts) == 2 else ''
    if action not in ('list', 'save', 'use', 'delete'):
        raise ValueError('Используйте /profile list, /profile save NAME, /profile use NAME или /profile delete NAME.')
    db = store.db
    with db:
        db.execute('BEGIN IMMEDIATE')
        old = db.execute('SELECT response FROM profile_commands WHERE update_id=?', (update_id,)).fetchone()
        if old:
            return old['response']
        try:
            profiles = _load(store)
            active = store.get_state(ACTIVE_KEY, '')
            if action == 'list':
                if value:
                    raise ValueError('Команда: /profile list без аргументов.')
                if not profiles:
                    result = 'Пока нет профилей. Сохранить текущие настройки: /profile save ИБ СПб.'
                else:
                    lines = ['<b>Профили поиска</b>']
                    for name in sorted(profiles, key=str.casefold):
                        marker = ' • редактируется' if name == active else ''
                        lines.append(f'• {escape(name)}{marker}')
                    lines.append('Все сохранённые профили ищутся одновременно; /profile use выбирает набор для редактирования через /set. Источники и расписание общие.')
                    result = '\n'.join(lines)
            elif action == 'save':
                name = _name(value)
                key = name.casefold()
                existing = next((entry for entry in profiles if entry.casefold() == key), None)
                if existing is None and len(profiles) >= MAX_PROFILES:
                    raise ValueError(f'Можно сохранить не больше {MAX_PROFILES} профилей.')
                if existing and existing != name:
                    del profiles[existing]
                profiles[name] = {'filters': monitor.config['filters']}
                _save(db, profiles)
                _write_state(db, ACTIVE_KEY, name)
                result = f'Текущие фильтры сохранены в профиле «{escape(name)}».'
            elif action == 'use':
                name = _name(value)
                selected = next((entry for entry in profiles if entry.casefold() == name.casefold()), None)
                if selected is None:
                    raise ValueError('Такого профиля нет. Список: /profile list.')
                overrides = json.loads(store.get_state(KEY, '{}'))
                if not isinstance(overrides, dict):
                    raise ValueError('Сохранённые настройки повреждены.')
                overrides['filters'] = profiles[selected]['filters']
                class Candidate:
                    def get_state(self, key, default=None):
                        return json.dumps(overrides, ensure_ascii=False) if key == KEY else default
                effective_config(monitor.base_config, Candidate())
                _write_state(db, KEY, json.dumps(overrides, ensure_ascii=False))
                _write_state(db, ACTIVE_KEY, selected)
                result = f'Профиль «{escape(selected)}» выбран для редактирования; поиск продолжится по всем сохранённым профилям.'
            else:
                name = _name(value)
                selected = next((entry for entry in profiles if entry.casefold() == name.casefold()), None)
                if selected is None:
                    raise ValueError('Такого профиля нет. Список: /profile list.')
                del profiles[selected]
                _save(db, profiles)
                if active == selected:
                    _write_state(db, ACTIVE_KEY, '')
                result = f'Профиль «{escape(selected)}» удалён.'
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            result = escape(str(exc))
        db.execute('INSERT INTO profile_commands VALUES (?, ?)', (update_id, result))
        return result


def handle_profile(telegram, monitor, update):
    if telegram.command(update) not in ('/profile', '/profiles'):
        return False
    update_id = update.get('update_id')
    if type(update_id) is not int or update_id < 0:
        telegram.send('Не удалось определить команду. Отправьте её ещё раз.')
        return True
    try:
        arguments = '' if telegram.command(update) == '/profiles' else telegram.arguments(update)
        response = _response(monitor.store, arguments, update_id, monitor)
    except sqlite3.Error:
        telegram.send('Не удалось сохранить профиль. Повторите команду позже.')
        return True
    monitor.reload_settings()
    if not arguments or arguments == 'list':
        from .workflows import profile_keyboard
        telegram.send(response, reply_markup=profile_keyboard(monitor))
    else:
        telegram.send(response)
    return True
