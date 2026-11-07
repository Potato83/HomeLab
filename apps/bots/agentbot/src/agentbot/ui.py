"""Plain-text cards and native Telegram controls; no transport or task data storage."""
import math
import time
import unicodedata
from collections.abc import Mapping, Sequence
from urllib.parse import urlsplit

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)


NEW_TASK = '➕ Задача'
NEW_BOARD = '🌐 Борда'
MY_TASKS = '📋 Мои задачи'
STATUS = '📍 Статус'
STOP = '⏹ Остановить'
HELP = '❔ Помощь'
PAGE_SIZE = 8

HELP_TEXT = (
    'Выберите действие на клавиатуре ниже.\n\n'
    '➕ Задача — название, условие и файлы, затем «Начать».\n'
    '🌐 Борда — адрес CTF, доступ и режим отправки флагов.\n'
    '📋 Мои задачи — выбор по короткому номеру и названию.\n'
    '📍 Статус — работа и последний ответ активной задачи.\n'
    '⏹ Остановить — остановка активной задачи.\n\n'
    'Обычное сообщение продолжает выбранную задачу. '
    'Длинный ID сессии вводить не нужно.\n\n'
    'Команды тоже работают: /new, /sessions, /use <номер>, '
    '/status, /result, /stop, /help, /cancel.'
)

STATE_LABELS = {
    'prepared': '⏳ Сохранена, ожидает отправки',
    'submitting': '📤 Отправляется агенту',
    'unknown': '⚠️ Отправка пока не подтверждена',
    'running': '🔎 Агент работает',
    'done': '✅ Завершена',
    'error': '⚠️ Ошибка агента',
    'stopped': '⏹ Остановлена',
}


def _data(row) -> dict:
    return dict(row) if row is not None else {}


def _single_line(value: str) -> str:
    text = ' '.join(str(value).split())
    return ''.join(c for c in text if unicodedata.category(c) not in {'Cc', 'Cf'})


def _compact(value: str, limit: int) -> str:
    text = _single_line(value)
    return text if len(text) <= limit else text[:max(0, limit - 1)].rstrip() + '…'


def _number(session) -> int:
    value = _data(session)['number']
    if isinstance(value, bool):
        raise ValueError('Invalid task number')
    number = int(value)
    if not 0 < number <= 2**63 - 1:
        raise ValueError('Invalid task number')
    return number


def task_label(session, limit: int = 80) -> str:
    """Return a public-facing short number/title, never the backend session ID."""
    data = _data(session)
    title = _compact(data.get('title') or 'Без названия', 60)
    return _compact(f'#{_number(data)} · {title}', max(24, limit))


def safe_board_host(url: str | None) -> str:
    """Display only a host/port; URL credentials, path and query never reach a card."""
    if not url:
        return 'не задан'
    try:
        if any(unicodedata.category(c) in {'Cc', 'Cf'} for c in url):
            return 'адрес не распознан'
        parsed = urlsplit(url)
        host = parsed.hostname
        port = parsed.port
        if parsed.scheme not in {'http', 'https'} or not host or any(c.isspace() for c in host):
            return 'адрес не распознан'
        host = f'[{host}]' if ':' in host else host
        return _compact(host + (f':{port}' if port else ''), 100)
    except (TypeError, ValueError):
        return 'адрес не распознан'


def _age(created, now: float) -> str | None:
    try:
        elapsed = now - float(created)
        if not math.isfinite(elapsed):
            return None
        elapsed = max(0, int(elapsed))
    except (TypeError, ValueError):
        return None
    if elapsed < 60:
        return f'{elapsed} с'
    if elapsed < 3600:
        return f'{elapsed // 60} м {elapsed % 60} с'
    return f'{elapsed // 3600} ч {(elapsed % 3600) // 60} м'


def card_text(session, run=None, *, files_count: int = 0,
              board_url: str | None = None, now: float | None = None,
              draft: bool = False, access_mode: str | None = None,
              allow_submit: bool | None = None) -> str:
    """Render only explicit safe fields. Send with parse_mode=None."""
    data, run_data = _data(session), _data(run)
    board = data.get('kind') == 'board'
    lines = [task_label(data), '🌐 CTF-борда' if board else '🧩 CTF-задача', '']
    state = run_data.get('state')
    if draft or not run_data:
        lines.append('📝 Черновик · ещё не запущена')
    else:
        lines.append(STATE_LABELS.get(state, 'Состояние уточняется'))
        age = _age(run_data.get('created'), time.time() if now is None else now)
        if age is not None:
            lines.append('Сохранена ' + age + ' назад')
    lines.append('Файлы: ' + str(max(0, int(files_count))))
    if board:
        lines.append('Борда: ' + safe_board_host(board_url))
        access = {'none': 'без входа', 'password': 'логин и пароль сохранены',
                  'token': 'API-токен сохранён'}.get(access_mode)
        if access:
            lines.append('Доступ: ' + access)
        if allow_submit is not None:
            lines.append('Флаги: ' + ('отправлять после проверки' if allow_submit else 'только показывать'))
    if run_data.get('stop_requested') and state not in {'done', 'error', 'stopped'}:
        lines.append('Остановка запрошена · ожидаем подтверждение')
    if state == 'unknown':
        lines.append('Автоповтора нет — задача сохранена.')
    lines.append('')
    lines.append('Добавьте материалы и нажмите «Начать».' if draft else
                 'Сообщение в чат продолжит эту задачу.')
    return '\n'.join(lines)


def _button(text: str, callback: str) -> InlineKeyboardButton:
    if not 1 <= len(callback.encode()) <= 64:
        raise ValueError('Invalid callback length')
    return InlineKeyboardButton(text=text, callback_data=callback)


def menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=NEW_TASK), KeyboardButton(text=NEW_BOARD)],
                  [KeyboardButton(text=MY_TASKS), KeyboardButton(text=STATUS)],
                  [KeyboardButton(text=STOP), KeyboardButton(text=HELP)]],
        resize_keyboard=True, is_persistent=True,
        input_field_placeholder='Условие задачи или продолжение диалога',
    )


def card_keyboard(session, *, draft: bool = False, running: bool = False) -> InlineKeyboardMarkup:
    number = _number(session)
    if draft:
        rows = [[_button('▶️ Начать', f'start:{number}'), _button('📎 Добавить файлы', f'addfiles:{number}')],
                [_button('✏️ Название', f'rename:{number}'), _button('↩️ Отмена', f'cancel:{number}')]]
    else:
        rows = [[_button('📍 Обновить', f'status:{number}'), _button('📄 Ответ', f'result:{number}')]]
        if running:
            rows.append([_button('⏹ Остановить', f'stop:{number}')])
        rows.append([_button('✏️ Название', f'rename:{number}'), _button('📋 Мои задачи', 'list:0')])
    rows.append([_button('🏠 Меню', 'menu')])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def sessions_keyboard(sessions: Sequence[Mapping], page: int = 0,
                      current_number: int | None = None) -> InlineKeyboardMarkup:
    """A compact picker; each task occupies one full-width row."""
    last_page = max(0, (len(sessions) - 1) // PAGE_SIZE)
    page = min(max(0, int(page)), last_page)
    visible = sessions[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    rows = []
    for session in visible:
        number = _number(session)
        prefix = '✓ ' if number == current_number else ''
        rows.append([_button(prefix + task_label(session, limit=60 - len(prefix)), f'job:{number}')])
    nav = []
    if page:
        nav.append(_button('‹ Назад', f'list:{page - 1}'))
    if page < last_page:
        nav.append(_button('Далее ›', f'list:{page + 1}'))
    if nav:
        rows.append(nav)
    rows.append([_button('🏠 Меню', 'menu')])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def wizard_keyboard(step: str, number: int | None = None) -> InlineKeyboardMarkup:
    """Controls for intake; callbacks contain no task text or access credentials."""
    if step in {'task_files', 'files'}:
        rows = [[_button('✅ Файлы добавлены / без файлов', 'donefiles')]]
    elif step == 'access':
        rows = [[_button('🔐 Логин и пароль', 'access_password')],
                [_button('🔑 API-токен', 'access_token'), _button('Без входа', 'access_none')]]
    elif step == 'submit':
        rows = [[_button('Только решать', 'submit_no')],
                [_button('Решать и отправлять флаги', 'submit_yes')]]
    elif step == 'review':
        rows = [[_button('▶️ Начать', 'start'), _button('📎 Добавить файлы', 'addfiles')]]
    else:
        rows = []
    rows.append([_button('↩️ Отмена', 'cancel')])
    if number is not None:
        for row in rows:
            for button in row:
                button.callback_data += f':{int(number)}'
    return InlineKeyboardMarkup(inline_keyboard=rows)
