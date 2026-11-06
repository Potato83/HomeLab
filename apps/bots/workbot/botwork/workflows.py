"""Short owner-only input flows for profiles and common search filters."""
from hashlib import sha256
from html import escape
import json
import time

from .interface import panel_token, settings_panel
from .profiles import _load, _name, _response
from .settings import Settings

PROMPTS = {
    'profile': 'Введите название нового профиля (до 32 символов). Текущие фильтры будут сохранены в нём.',
    'city': 'Введите города через |, например Санкт-Петербург | Москва.',
    'include': 'Введите нужные слова через |. Для очистки отправьте -.',
    'exclude': 'Введите слова-исключения через |. Для очистки отправьте -.',
    'min_salary': 'Введите минимальную зарплату в рублях в месяц или off.',
    'schedule': 'Введите расписание по Москве, например mon,tue,wed,thu,fri 09:00-18:00, или off.',
    'career_note': 'Введите заметку по вакансии. Для очистки отправьте -.',
    'career_contact': 'Введите контакт рекрутера. Для очистки отправьте -.',
    'career_next': 'Введите следующий шаг по вакансии. Для очистки отправьте -.',
    'career_remind': 'Введите дату и текст: ГГГГ-ММ-ДД ЧЧ:ММ текст (МСК). Или off для удаления напоминания, deadline для дедлайна источника.',
}


def begin_input(telegram, monitor, kind, ref=None):
    monitor.store.set_state('input_request', json.dumps({'kind': kind, 'ref': ref, 'token': panel_token(monitor), 'expires': time.time() + 600}))
    telegram.send(PROMPTS[kind] + '\n\nОтмена: /cancel. Ожидание — 10 минут.')


def handle_input(telegram, monitor, update):
    message = update.get('message')
    if not isinstance(message, dict) or not isinstance(message.get('chat'), dict):
        return False
    if str(message['chat'].get('id')) != telegram.chat_id or message['chat'].get('type') != 'private':
        return False
    pending = json.loads(monitor.store.get_state('input_request', '{}'))
    command = telegram.command(update)
    if command == '/cancel':
        monitor.store.set_state('input_request', '{}')
        telegram.send('Ввод отменён. /menu')
        return True
    if pending and command:
        monitor.store.set_state('input_request', '{}')
        return False
    if not pending or not isinstance(message.get('text'), str):
        return False
    if pending['expires'] < time.time() or pending['token'] != panel_token(monitor):
        monitor.store.set_state('input_request', '{}')
        telegram.send('Ожидание завершено или настройки изменились. Откройте нужную кнопку заново.')
        return True
    value, kind = message['text'].strip(), pending['kind']
    try:
        if kind.startswith('career_'):
            from .career import execute_career, send_application
            response = execute_career(monitor.store, '/' + kind.removeprefix('career_'), pending['ref'] + ' ' + value, update['update_id'])
            monitor.store.set_state('input_request', '{}')
            telegram.send(escape(response))
            send_application(telegram, monitor.store, pending['ref'])
            return True
        if kind == 'profile':
            name = _name(value)
            if any(name.casefold() == old.casefold() for old in _load(monitor.store)):
                raise ValueError('Такое имя уже есть. Введите другое или /cancel.')
            response = _response(monitor.store, 'save ' + name, update['update_id'], monitor)
        else:
            response = Settings(monitor.base_config, monitor.store).execute('/set', kind + ' ' + value, update['update_id'])
        monitor.reload_settings()
        monitor.store.set_state('input_request', '{}')
        telegram.send(response)
        text, keyboard = settings_panel(monitor)
        telegram.send(text, reply_markup=keyboard)
    except ValueError as exc:
        telegram.send(escape(str(exc)))
    return True


def handle_application_buttons(telegram, monitor, update):
    callback = telegram.owner_callback(update)
    if not callback or not isinstance(callback.get('data'), str) or not callback['data'].startswith('app:'):
        return False
    parts = callback['data'].split(':')
    try:
        if len(parts) != 3 or parts[1] not in ('note', 'contact', 'next', 'remind') or not monitor.store.vacancy_by_ref(parts[2]):
            raise ValueError('Вакансия не найдена')
    except ValueError:
        telegram.answer_callback(callback['id'], 'Вакансия не найдена')
        return True
    telegram.answer_callback(callback['id'], 'Ожидаю ввод')
    begin_input(telegram, monitor, 'career_' + parts[1], parts[2])
    return True


def profile_keyboard(monitor):
    token = panel_token(monitor)
    rows = []
    for name in sorted(_load(monitor.store), key=str.casefold):
        ref = sha256(name.encode()).hexdigest()[:12]
        rows.append([{'text': '✏️ ' + name, 'callback_data': f'pr:use:{ref}:{token}'},
                     {'text': 'Удалить', 'callback_data': f'pr:ask:{ref}:{token}'}])
    rows.append([{'text': '＋ Сохранить новый профиль', 'callback_data': 'pr:new:' + token}])
    return {'inline_keyboard': rows}


def handle_profile_buttons(telegram, monitor, update):
    callback = telegram.owner_callback(update)
    if not callback or not isinstance(callback.get('data'), str) or not callback['data'].startswith('pr:'):
        return False
    action, _, token = callback['data'].rpartition(':')
    if token != panel_token(monitor):
        telegram.answer_callback(callback['id'], 'Список изменился. Откройте /profiles заново.')
        return True
    if action == 'pr:new':
        telegram.answer_callback(callback['id'], 'Введите название')
        begin_input(telegram, monitor, 'profile')
        return True
    parts = action.split(':')
    names = [name for name in _load(monitor.store) if len(parts) == 3 and sha256(name.encode()).hexdigest()[:12] == parts[2]]
    if len(names) != 1 or parts[1] not in ('use', 'ask', 'delete'):
        telegram.answer_callback(callback['id'], 'Профиль не найден')
        return True
    name = names[0]
    if parts[1] == 'ask':
        telegram.answer_callback(callback['id'], 'Подтвердите удаление')
        telegram.send('Удалить профиль «' + escape(name) + '»?', reply_markup={'inline_keyboard': [[
            {'text': 'Да, удалить', 'callback_data': f'pr:delete:{parts[2]}:{token}'}]]})
    else:
        response = _response(monitor.store, ('use ' if parts[1] == 'use' else 'delete ') + name, update['update_id'], monitor)
        monitor.reload_settings()
        telegram.answer_callback(callback['id'], 'Готово')
        telegram.send(response)
        if parts[1] == 'use':
            text, keyboard = settings_panel(monitor)
            telegram.send(text, reply_markup=keyboard)
        else:
            telegram.send('Профили поиска', reply_markup=profile_keyboard(monitor))
    return True
