"""Owner-managed application notes, reminders and spreadsheet export."""
import csv
from datetime import datetime, timedelta, timezone
from html import escape
import io

from .pipeline import STATUSES
from .schedule import MOSCOW
from .storage import now


def reminder_time(value):
    try:
        return datetime.strptime(value, '%Y-%m-%d %H:%M').replace(tzinfo=MOSCOW).astimezone(timezone.utc)
    except ValueError:
        raise ValueError('Дата: ГГГГ-ММ-ДД ЧЧ:ММ по Москве.') from None


def execute_career(store, command, arguments, update_id):
    if type(update_id) is not int or update_id < 0:
        raise ValueError('Отправьте команду ещё раз.')
    parts = arguments.split(maxsplit=1)
    if len(parts) != 2:
        raise ValueError(f'{command} ID текст; /remind ID ГГГГ-ММ-ДД ЧЧ:ММ текст | off | deadline')
    ref, value = parts
    vacancy = store.vacancy_by_ref(ref)
    if not vacancy:
        raise ValueError('Вакансия не найдена. ID указан в карточке.')
    if len(value) > 1500:
        raise ValueError('Допустимо до 1500 символов.')
    with store.db:
        store.db.execute('BEGIN IMMEDIATE')
        old = store.db.execute('SELECT response FROM career_commands WHERE update_id=?', (update_id,)).fetchone()
        if old:
            return old['response']
        if command in ('/note', '/contact', '/next'):
            column = {'/note': 'note', '/contact': 'contact', '/next': 'next_action'}[command]
            store.db.execute('INSERT OR IGNORE INTO career_details(ref) VALUES (?)', (ref,))
            store.db.execute(f'UPDATE career_details SET {column}=? WHERE ref=?', ('' if value == '-' else value, ref))
            response = 'Запись сохранена. Посмотреть: /application ' + ref
        elif command == '/remind':
            if value == 'off':
                store.db.execute('DELETE FROM reminders WHERE ref=?', (ref,))
                response = 'Напоминание удалено.'
            else:
                if value == 'deadline':
                    try:
                        deadline = datetime.fromisoformat(vacancy.get('registration_deadline', '').replace('Z', '+00:00'))
                        if deadline.tzinfo is None or deadline <= datetime.now(timezone.utc):
                            raise ValueError
                    except ValueError:
                        raise ValueError('Действующий дедлайн не указан источником. Задайте дату вручную.') from None
                    due = max(deadline - timedelta(days=1), datetime.now(timezone.utc))
                    text = 'Приближается дедлайн подачи заявки'
                else:
                    fields = value.split(maxsplit=2)
                    if len(fields) < 2:
                        raise ValueError('Пример: /remind ID 2026-12-01 18:00 Проверить ответ')
                    due = reminder_time(' '.join(fields[:2]))
                    text = fields[2] if len(fields) == 3 else 'Вернуться к вакансии'
                    if due <= datetime.now(timezone.utc):
                        raise ValueError('Укажите будущую дату.')
                store.db.execute('''INSERT INTO reminders VALUES (?, ?, ?, NULL)
                    ON CONFLICT(ref) DO UPDATE SET due_at=excluded.due_at, text=excluded.text, delivered_at=NULL''',
                    (ref, due.astimezone(timezone.utc).isoformat(), text))
                response = 'Напомню ' + due.astimezone(MOSCOW).strftime('%d.%m.%Y в %H:%M МСК')
        else:
            raise ValueError('Неизвестная команда.')
        store.db.execute('INSERT OR IGNORE INTO tracked_vacancies VALUES (?, ?, ?, ?, ?)',
                         (ref, vacancy['source'], vacancy['external_id'], 'saved', now()))
        store.db.execute('INSERT INTO career_commands VALUES (?, ?)', (update_id, response))
        return response


def application_blocks(store, ref):
    vacancy = store.vacancy_by_ref(ref)
    if not vacancy:
        raise ValueError('Вакансия не найдена.')
    details = store.db.execute('SELECT * FROM career_details WHERE ref=?', (ref,)).fetchone()
    reminder = store.db.execute('SELECT * FROM reminders WHERE ref=? AND delivered_at IS NULL', (ref,)).fetchone()
    status = store.db.execute('SELECT status FROM tracked_vacancies WHERE ref=?', (ref,)).fetchone()
    lines = ['<b>' + escape(vacancy['title'][:200]) + '</b>', escape(vacancy['company'][:120]),
             'Статус: ' + (STATUSES[status['status']] if status else 'не сохранена')]
    for field, label in [('note', 'Заметка'), ('contact', 'Контакт'), ('next_action', 'Следующее действие')]:
        if details and details[field]:
            lines.append(label + ': ' + escape(details[field]))
    if vacancy.get('registration_deadline'):
        try:
            deadline = datetime.fromisoformat(vacancy['registration_deadline'].replace('Z', '+00:00'))
            if deadline.tzinfo is None:
                raise ValueError
            lines.append('Дедлайн источника: ' + deadline.astimezone(MOSCOW).strftime('%d.%m.%Y %H:%M МСК'))
            lines.append(f'<code>/remind {ref} deadline</code> — напомнить за день до него')
        except ValueError:
            lines.append('Дедлайн источника не подтверждён.')
    if reminder:
        lines.append('Напоминание: ' + datetime.fromisoformat(reminder['due_at']).astimezone(MOSCOW).strftime('%d.%m.%Y %H:%M МСК'))
    lines.append(f'<a href="{escape(vacancy["url"], quote=True)}">Открыть вакансию</a>')
    lines.append(f'<code>/note {ref} текст</code>\n<code>/contact {ref} контакт</code>\n<code>/next {ref} действие</code>\n<code>/remind {ref} ГГГГ-ММ-ДД ЧЧ:ММ текст</code>')
    return lines


def application_text(store, ref):
    return '\n\n'.join(application_blocks(store, ref))


def send_application(telegram, store, ref):
    from .sites import length
    blocks, chunk = application_blocks(store, ref), ''
    for block in blocks:
        if chunk and length(chunk + '\n\n' + block) > 3300:
            telegram.send(chunk)
            chunk = ''
        chunk += ('\n\n' if chunk else '') + block
    buttons = [[{'text': label, 'callback_data': f'app:{action}:{ref}'} for action, label in group] for group in
               [[('note', 'Заметка'), ('contact', 'Контакт')], [('next', 'Следующий шаг'), ('remind', 'Напомнить')]]]
    telegram.send(chunk, reply_markup={'inline_keyboard': buttons})


def reminders_page(store, page=1):
    count = store.db.execute('SELECT COUNT(*) FROM reminders WHERE delivered_at IS NULL').fetchone()[0]
    pages = max(1, (count + 7) // 8)
    page = min(max(1, page), pages)
    rows = store.db.execute('SELECT * FROM reminders WHERE delivered_at IS NULL ORDER BY due_at LIMIT 8 OFFSET ?', ((page - 1) * 8,)).fetchall()
    lines = [f'<b>Мои напоминания · {count}</b>', f'Страница {page}/{pages}']
    for row in rows:
        date = datetime.fromisoformat(row['due_at']).astimezone(MOSCOW).strftime('%d.%m.%Y %H:%M')
        lines.append(f'{date} МСК · {escape(row["text"][:70])}\n<code>/application {row["ref"]}</code>')
    if not rows:
        lines.append('Пока пусто. Добавить: /remind ID ГГГГ-ММ-ДД ЧЧ:ММ текст')
    navigation = []
    if page > 1:
        navigation.append({'text': '← Назад', 'callback_data': f'rem:list:{page - 1}'})
    if page < pages:
        navigation.append({'text': 'Дальше →', 'callback_data': f'rem:list:{page + 1}'})
    return '\n\n'.join(lines), {'inline_keyboard': [navigation]} if navigation else None


def deliver_reminders(store, telegram):
    rows = store.db.execute('''SELECT * FROM reminders WHERE delivered_at IS NULL AND due_at<=?
        AND NOT EXISTS (SELECT 1 FROM tracked_vacancies t WHERE t.ref=reminders.ref AND t.status='ignored')
        ORDER BY due_at LIMIT 5''', (now(),)).fetchall()
    for row in rows:
        vacancy = store.vacancy_by_ref(row['ref'])
        title = vacancy['title'] if vacancy else row['ref']
        telegram.send('⏰ <b>Напоминание</b>\n' + escape(title[:200]) + '\n\n' + escape(row['text'][:1500]) +
                      f'\n\n<code>/application {row["ref"]}</code>')
        with store.db:
            store.db.execute('UPDATE reminders SET delivered_at=? WHERE ref=? AND due_at=?', (now(), row['ref'], row['due_at']))
    return len(rows)


def export_csv(store):
    output = io.StringIO(newline='')
    writer = csv.writer(output, delimiter=';')
    writer.writerow(['ID', 'Статус', 'Вакансия', 'Компания', 'Ссылка', 'Заметка', 'Контакт', 'Следующее действие', 'Напоминание UTC'])
    for ignored in (False, True):
        for row in store.tracked_vacancies(ignored=ignored):
            details = store.db.execute('SELECT * FROM career_details WHERE ref=?', (row['ref'],)).fetchone()
            reminder = store.db.execute('SELECT due_at FROM reminders WHERE ref=? AND delivered_at IS NULL', (row['ref'],)).fetchone()
            values = [row['ref'], STATUSES[row['status']], row['title'], row['company'], row['url'],
                      *(details[key] if details else '' for key in ('note', 'contact', 'next_action')), reminder['due_at'] if reminder else '']
            # Spreadsheet applications may interpret remote text as formulas.
            writer.writerow(["'" + value if value.lstrip('\ufeff \t\r\n').startswith(('=', '+', '-', '@')) else value for value in values])
            if output.tell() > 900_000:
                raise ValueError('Экспорт слишком большой для одного файла.')
    return output.getvalue().encode('utf-8-sig')


def handle_career(telegram, store, update):
    command = telegram.command(update)
    callback = telegram.owner_callback(update)
    if callback and isinstance(callback.get('data'), str) and callback['data'].startswith('rem:list:'):
        value = callback['data'].removeprefix('rem:list:')
        if not value.isdigit() or len(value) > 6:
            telegram.answer_callback(callback['id'], 'Неизвестная страница')
            return True
        telegram.answer_callback(callback['id'], 'Открываю страницу')
        text, keyboard = reminders_page(store, int(value))
        telegram.send(text, reply_markup=keyboard)
        return True
    if command not in ('/note', '/contact', '/next', '/remind', '/reminders', '/application', '/track_export'):
        return False
    try:
        if command == '/track_export':
            telegram.send_csv('botwork-applications.csv', export_csv(store))
            return True
        if command == '/application':
            ref = telegram.arguments(update).strip()
            send_application(telegram, store, ref)
            return True
        elif command == '/reminders':
            text, keyboard = reminders_page(store)
            telegram.send(text, reply_markup=keyboard)
            return True
        else:
            response = escape(execute_career(store, command, telegram.arguments(update), update.get('update_id')))
    except ValueError as exc:
        response = escape(str(exc))
    telegram.send(response)
    return True
