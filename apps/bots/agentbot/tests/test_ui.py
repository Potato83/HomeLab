import sqlite3

import pytest

from agentbot.ui import (
    HELP_TEXT,
    card_keyboard,
    card_text,
    menu_keyboard,
    safe_board_host,
    sessions_keyboard,
    task_label,
    wizard_keyboard,
)


def session(number=3, title='Weird RSA', **extra):
    return {'id': 'ses_private_backend_identifier', 'number': number,
            'title': title, 'kind': 'task', **extra}


def callbacks(markup):
    return [button.callback_data for row in markup.inline_keyboard for button in row]


def test_short_number_title_replaces_backend_id():
    assert task_label(session()) == '#3 · Weird RSA'
    assert 'ses_' not in card_text(session())


def test_title_is_single_line_and_bounds_mobile_button_length():
    row = session(title='Very\nlong\r title\t' + '語' * 300 + '\u202e')
    text = task_label(row)
    assert '\n' not in text and '\r' not in text and '\t' not in text
    assert '\u202e' not in text and len(text.split(' · ', 1)[1]) <= 60
    button = sessions_keyboard([row], current_number=3).inline_keyboard[0][0]
    assert len(button.text) <= 60 and button.text.startswith('✓ #3')
    assert button.callback_data == 'job:3'


def test_sqlite_row_supported():
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    row = db.execute("SELECT 3 AS number, 'Named task' AS title, 'task' AS kind").fetchone()
    assert task_label(row) == '#3 · Named task'
    db.close()


@pytest.mark.parametrize('number', [0, -1, True, 2**63])
def test_invalid_numeric_identifier_cannot_enter_callback(number):
    with pytest.raises(ValueError):
        card_keyboard(session(number=number))


def test_card_does_not_render_description_credentials_or_backend_ids():
    row = session(kind='board', title='Training board', description='PRIVATE_TASK_TEXT',
                  credentials={'password': 'PRIVATE_PASSWORD'}, token='PRIVATE_TOKEN')
    run = {'state': 'running', 'created': 100, 'prompt': 'PRIVATE_PROMPT', 'answer': 'PRIVATE_ANSWER'}
    text = card_text(row, run, now=170, files_count=2,
                     board_url='https://login:URL_PASSWORD@ctf.example:8443/path-secret?token=URL_TOKEN#fragment',
                     access_mode='password', allow_submit=False)
    assert '#3 · Training board' in text and 'Агент работает' in text
    assert '1 м 10 с' in text and 'Файлы: 2' in text
    assert 'ctf.example:8443' in text and 'только показывать' in text
    for private in ['PRIVATE_', 'URL_PASSWORD', 'URL_TOKEN', 'path-secret', 'login:', 'ses_']:
        assert private not in text


@pytest.mark.parametrize(('url', 'expected'), [
    ('https://user:password@ctf.example/a?token=secret', 'ctf.example'),
    ('http://[::1]:8000/path', '[::1]:8000'),
    ('file:///tmp/private', 'адрес не распознан'),
    ('https://ctf.example:not-a-port/', 'адрес не распознан'),
    ('https://ctf.example\nspoofed/', 'адрес не распознан'),
    (None, 'не задан'),
])
def test_only_safe_board_host_shown(url, expected):
    assert safe_board_host(url) == expected


def test_unknown_outcome_and_pending_stop_remain_clear():
    text = card_text(session(), {'state': 'unknown', 'created': 200, 'stop_requested': 1}, now=100)
    assert '0 с' in text
    assert 'Автоповтора нет' in text
    assert 'Остановка запрошена' in text


def test_main_keyboard_permanent_and_all_actions_visible():
    keyboard = menu_keyboard()
    assert keyboard.is_persistent and keyboard.resize_keyboard
    assert [[button.text for button in row] for row in keyboard.keyboard] == [
        ['➕ Задача', '🌐 Борда'], ['📋 Мои задачи', '📍 Статус'], ['⏹ Остановить', '❔ Помощь']]
    assert '/use <номер>' in HELP_TEXT


def test_card_and_wizard_actions_use_only_short_fixed_callbacks():
    options = [card_keyboard(session(), running=True), card_keyboard(session(), draft=True)]
    options += [wizard_keyboard(step) for step in ['files', 'access', 'submit', 'review', 'text']]
    all_callbacks = [value for option in options for value in callbacks(option)]
    for expected in ['status:3', 'result:3', 'stop:3', 'rename:3', 'start', 'addfiles',
                     'access_password', 'access_token', 'access_none', 'submit_yes',
                     'submit_no', 'cancel', 'donefiles', 'menu']:
        assert expected in all_callbacks
    assert all(len(value.encode()) <= 64 and 'ses_' not in value for value in all_callbacks)


def test_task_picker_has_eight_rows_and_bidirectional_pagination():
    rows = [session(number=n, title=f'Task {n}') for n in range(1, 20)]
    first = callbacks(sessions_keyboard(rows, page=0))
    middle = callbacks(sessions_keyboard(rows, page=1))
    last = callbacks(sessions_keyboard(rows, page=999))
    assert first[:8] == [f'job:{n}' for n in range(1, 9)]
    assert 'list:1' in first and 'list:-1' not in first
    assert middle[:8] == [f'job:{n}' for n in range(9, 17)]
    assert 'list:0' in middle and 'list:2' in middle
    assert last[:3] == [f'job:{n}' for n in range(17, 20)]
    assert 'list:1' in last and 'list:3' not in last


def test_empty_picker_and_negative_page_are_safe():
    assert callbacks(sessions_keyboard([], page=-5)) == ['menu']
    assert callbacks(sessions_keyboard([session()], page=-5)) == ['job:3', 'menu']
