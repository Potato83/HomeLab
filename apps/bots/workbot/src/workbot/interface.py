"""Compact Telegram navigation and explicit, replay-safe settings actions."""
from html import escape
from hashlib import sha256
import json
import sqlite3

NAVIGATION = {
    '🔎 Проверить': '/check', '⚙️ Настройки': '/settings',
    '📌 Мои вакансии': '/track', '🌐 Каталог': '/sites',
    '📡 Состояние': '/status', '📊 Сводка': '/insights',
    '📋 План на сегодня': '/today', '📚 Помощь': '/help',
    '💼 Вакансии': '/jobs', '⏰ Напоминания': '/reminders',
    '🗂 Профили': '/profiles', '📤 Экспорт откликов': '/track_export',
}

HOME_KEYBOARD = {'keyboard': [[{'text': label} for label in list(NAVIGATION)[i:i + 2]]
                              for i in range(0, len(NAVIGATION), 2)],
                 'resize_keyboard': True, 'is_persistent': True}


def home_text(config):
    paused = config['monitor'].get('paused', False)
    return ('<b>WorkBot · поиск работы</b>\n\n' +
            ('⏸ Мониторинг на паузе' if paused else '🟢 Мониторинг включён') +
            '\n\n🔎 Проверить — запустить поиск сейчас\n'
            '📌 Мои вакансии — сохранённое и отклики\n'
            '⚙️ Настройки — фильтры и уведомления\n'
            '🌐 Каталог — сайты и полезные ссылки\n\n'
            '💼 Вакансии — поиск по найденному\n'
            '⏰ Напоминания — личные сроки и действия\n'
            '🗂 Профили — создать или выбрать набор фильтров\n\n'
            '📋 План на сегодня — напоминания и следующие шаги\n\n'
            'Нажмите кнопку внизу. Все команды: /help')


def settings_panel(monitor):
    from .directions import LABELS, selected
    config = monitor.config
    f, m = config['filters'], config['monitor']
    active = monitor.store.get_state('active_search_profile', '') or 'Текущий поиск'
    formats = {'remote': 'удалённо', 'hybrid': 'гибрид', 'office': 'офис'}
    levels = {'intern': 'стажировки', 'junior': 'junior'}
    paused, digest, events = m.get('paused', False), m.get('delivery_mode') == 'digest', f.get('events_enabled', False)
    text = ('<b>⚙️ Настройки поиска</b>\n\n'
            f'Редактируется: <b>{escape(active)}</b>\n'
            'Направления: ' + ', '.join(LABELS[key] for key in selected(f)) + '\n'
            'Уровень: ' + ', '.join(levels[key] for key in f.get('allowed_levels', ['intern', 'junior'])) + '\n'
            'Города: ' + escape(f.get('city_label', 'Санкт-Петербург')[:180]) + '\n'
            'Формат: ' + ', '.join(formats[key] for key in f.get('allowed_formats', ['remote', 'hybrid', 'office'])) + '\n\n'
            + ('⏸ На паузе' if paused else '🟢 Мониторинг включён') + '\n'
            'Уведомления: ' + (f'подборка раз в {m.get("digest_seconds", 3600) // 60} мин.' if digest else 'сразу') + '\n'
            'Мероприятия в этом профиле: ' + ('включены' if events else 'выключены') + '\n\n'
            'Все сохранённые профили ищутся одновременно. Выбор для редактирования: /profile list\n'
            'Подробные фильтры и команды: /settings all')
    buttons = [[{'text': '▶️ Возобновить' if paused else '⏸ Пауза', 'callback_data': 'ui:pause:' + ('off' if paused else 'on')}],
               [{'text': '⚡ Присылать сразу' if digest else '📬 Получать подборку', 'callback_data': 'ui:delivery:' + ('instant' if digest else 'digest')}],
               [{'text': '📅 Выключить мероприятия' if events else '📅 Включить мероприятия', 'callback_data': 'ui:events:' + ('off' if events else 'on')}],
               [{'text': 'Только стажировки', 'callback_data': 'ui:levels:intern'},
                {'text': 'Стажировки + junior', 'callback_data': 'ui:levels:both'}],
               [{'text': 'Только удалённо', 'callback_data': 'ui:formats:remote'},
                {'text': 'Все форматы', 'callback_data': 'ui:formats:all'}],
               [{'text': 'Только ИБ', 'callback_data': 'ui:directions:security'},
                {'text': 'Все направления', 'callback_data': 'ui:directions:all'}],
               [{'text': 'Города', 'callback_data': 'ui:input:city'}, {'text': 'Зарплата', 'callback_data': 'ui:input:min_salary'}],
               [{'text': 'Ключевые слова', 'callback_data': 'ui:input:include'}, {'text': 'Исключения', 'callback_data': 'ui:input:exclude'}],
               [{'text': 'Расписание', 'callback_data': 'ui:input:schedule'}]]
    token = panel_token(monitor)
    for row in buttons:
        for button in row:
            button['callback_data'] += ':' + token
    return text, {'inline_keyboard': buttons}


def panel_token(monitor):
    state = {'config': monitor.config, 'active': monitor.store.get_state('active_search_profile', '')}
    return sha256(json.dumps(state, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:10]


def handle_controls(telegram, monitor, update):
    callback = telegram.owner_callback(update)
    if callback is None or not isinstance(callback.get('data'), str) or not callback['data'].startswith('ui:'):
        return False
    actions = {'ui:pause:on': 'pause on', 'ui:pause:off': 'pause off',
               'ui:delivery:instant': 'delivery instant', 'ui:delivery:digest': 'delivery digest',
               'ui:events:on': 'events on', 'ui:events:off': 'events off',
               'ui:levels:intern': 'levels intern', 'ui:levels:both': 'levels intern,junior',
               'ui:formats:remote': 'formats remote', 'ui:formats:all': 'formats remote,hybrid,office',
               'ui:directions:security': 'directions security',
               'ui:directions:all': 'directions security,devsecops,devops,mlops'}
    action, _, token = callback['data'].rpartition(':')
    if token != panel_token(monitor):
        telegram.answer_callback(callback['id'], 'Настройки уже изменились. Откройте /settings заново.')
        return True
    if action in ('ui:input:city', 'ui:input:min_salary', 'ui:input:include', 'ui:input:exclude', 'ui:input:schedule'):
        from .workflows import begin_input
        telegram.answer_callback(callback['id'], 'Ожидаю ввод')
        begin_input(telegram, monitor, action.split(':')[-1])
        return True
    arguments = actions.get(action)
    if arguments is None:
        telegram.answer_callback(callback['id'], 'Неизвестное действие')
        return True
    from .settings import Settings
    try:
        response = Settings(monitor.base_config, monitor.store).execute('/set', arguments, update['update_id'])
        monitor.reload_settings()
    except (sqlite3.Error, ValueError):
        telegram.answer_callback(callback['id'], 'Не удалось сохранить. Попробуйте ещё раз.')
        return True
    telegram.answer_callback(callback['id'], response[:180])
    text, keyboard = settings_panel(monitor)
    telegram.send(text, reply_markup=keyboard)
    return True
