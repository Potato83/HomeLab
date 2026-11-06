"""Personal application pipeline controlled through Telegram commands."""

from hashlib import sha256
from html import escape
import re

STATUSES = {
    'saved': 'Сохранена', 'applied': 'Откликнулся', 'interview': 'Собеседование',
    'offer': 'Оффер', 'rejected': 'Отказ', 'closed': 'Закрыта', 'ignored': 'Не подходит',
}


def vacancy_ref(source, external_id):
    return sha256((source + '\0' + external_id).encode()).hexdigest()[:8]


def feedback_buttons(vacancy, *, label=False):
    if vacancy.get('kind', 'vacancy') != 'vacancy':
        return []
    ref = vacancy_ref(vacancy['source'], vacancy['external_id'])
    save_label = '⭐ ' + vacancy['title'][:32] if label else '⭐ Сохранить'
    hide_label = 'Скрыть · ' + ref if label else '🚫 Не подходит'
    return [[{'text': save_label, 'callback_data': 'bw:s:' + ref},
             {'text': hide_label, 'callback_data': 'bw:i:' + ref}]]


def handle_feedback(telegram, store, update):
    callback = update.get('callback_query')
    if not isinstance(callback, dict):
        return False
    callback = telegram.owner_callback(update)
    if callback is None:
        return True
    callback_id, data, update_id = callback.get('id'), callback.get('data'), update.get('update_id')
    match = re.fullmatch(r'bw:([si]):([0-9a-f]{8})', data) if isinstance(data, str) else None
    if not match:
        telegram.answer_callback(callback_id, 'Неизвестное действие')
        return True
    status = 'saved' if match[1] == 's' else 'ignored'
    response = store.apply_feedback(update_id, match[2], status)
    telegram.answer_callback(callback_id, response)
    return True


def _track_text(store, arguments, mutate):
    parts = arguments.split()
    if not parts:
        rows = store.tracked_vacancies()
        if not rows:
            return ('Пока нет вакансий в откликах. Добавить: /track ID saved.\n'
                    'Дальше используйте applied, interview, offer, rejected или closed.')
        lines = ['<b>Мои вакансии</b>']
        for row in rows[:40]:
            lines.append(f"{STATUSES[row['status']]} · {escape(row['title'][:180])} · "
                         f"{escape(row['company'][:100])} · <a href=\"{escape(row['url'], quote=True)}\">открыть</a>\n"
                         f"ID: <code>{row['ref']}</code>")
        if len(rows) > 40:
            lines.append(f'Показаны первые 40 из {len(rows)}.')
        return '\n'.join(lines)
    if len(parts) != 2 or len(parts[0]) != 8:
        raise ValueError('Формат: /track ID saved|applied|interview|offer|rejected|closed|ignored|remove')
    ref, status = parts
    if status not in (*STATUSES, 'remove'):
        raise ValueError('Статус: saved, applied, interview, offer, rejected, closed, ignored или remove.')
    result = mutate(ref, status)
    if result is None:
        raise ValueError('Вакансия с таким ID не найдена. ID указан в карточке вакансии.')
    if status == 'remove':
        return f"Убрана из списка откликов: {escape(result['title'][:180])}."
    if status == 'ignored':
        return f"Вакансия «{escape(result['title'][:180])}» скрыта из будущих уведомлений. Вернуть: /track {ref} remove."
    return f"Статус «{STATUSES[status]}»: <a href=\"{escape(result['url'], quote=True)}\">{escape(result['title'][:180])}</a>."


def track_text(store, arguments, update_id=None):
    if update_id is None:
        return _track_text(store, arguments, store.set_tracking)
    if type(update_id) is not int or update_id < 0:
        raise ValueError('Отправьте команду ещё раз.')
    with store.db:
        store.db.execute('BEGIN IMMEDIATE')
        old = store.db.execute('SELECT response FROM career_commands WHERE update_id=?', (update_id,)).fetchone()
        if old:
            return old['response']
        response = _track_text(store, arguments, store._set_tracking)
        store.db.execute('INSERT INTO career_commands VALUES (?, ?)', (update_id, response))
        return response


def handle_track(telegram, store, update):
    callback = telegram.owner_callback(update)
    arguments = telegram.arguments(update)
    if callback and isinstance(callback.get('data'), str) and callback['data'].startswith('tr:'):
        match = re.fullmatch(r'tr:(all|ignored):([0-9]{1,6})', callback['data'])
        if not match:
            telegram.answer_callback(callback['id'], 'Неизвестная страница')
            return True
        arguments = ('ignored ' if match[1] == 'ignored' else '') + 'page=' + match[2]
        telegram.answer_callback(callback['id'], 'Открываю страницу')
    elif telegram.command(update) != '/track':
        return False
    try:
        listing = re.fullmatch(r'(ignored)?(?:\s*page=([0-9]{1,6}))?', arguments)
        if listing is None:
            telegram.send(track_text(store, arguments, update.get('update_id')))
            return True
        ignored = bool(listing[1])
        all_rows = store.tracked_vacancies(ignored=ignored)
        pages = max(1, (len(all_rows) + 7) // 8)
        page = min(max(1, int(listing[2] or 1)), pages)
        lines = ['<b>' + ('Скрытые вакансии' if ignored else 'Мои вакансии') + f'</b> · {len(all_rows)}', f'Страница {page}/{pages}']
        for row in all_rows[(page - 1) * 8:page * 8]:
            lines.append(f"{STATUSES[row['status']]} · <b>{escape(row['title'][:140])}</b>\n{escape(row['company'][:80])}\n"
                         f"<code>/application {row['ref']}</code>")
        if not all_rows:
            lines.append('Пока пусто. Найти вакансии: /jobs')
        lines.append('Заметки и напоминания: /application ID\nCSV всех откликов: /track_export')
        if ignored:
            lines.append('Вернуть: /track ID remove или /track ID saved.')
        kind = 'ignored' if ignored else 'all'
        buttons = []
        if page > 1:
            buttons.append({'text': '← Назад', 'callback_data': f'tr:{kind}:{page - 1}'})
        if page < pages:
            buttons.append({'text': 'Дальше →', 'callback_data': f'tr:{kind}:{page + 1}'})
        markup = {'inline_keyboard': [buttons]} if buttons else None
        telegram.send('\n\n'.join(lines), reply_markup=markup)
    except ValueError as exc:
        telegram.send(escape(str(exc)))
    return True
