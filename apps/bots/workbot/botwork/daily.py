"""Read-only daily agenda from the owner's recorded actions and reminders."""
from datetime import datetime, timedelta, timezone
from html import escape
import json

from .pipeline import STATUSES
from .schedule import MOSCOW
from .sites import length

VISIBLE_ITEMS = 3


def _title(row):
    payload = json.loads(row['payload']) if row['payload'] else {}
    return escape(payload.get('vacancy', {}).get('title', row['ref'])[:80])


def _preview(store, query, order_by, parameters=()):
    count = store.db.execute('SELECT COUNT(*) FROM (' + query + ')', parameters).fetchone()[0]
    rows = store.db.execute(query + ' ORDER BY ' + order_by + ' LIMIT ?', (*parameters, VISIBLE_ITEMS)).fetchall()
    return count, rows


def daily_blocks(store, current=None):
    current = current or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError('Для плана требуется дата с часовым поясом.')
    local = current.astimezone(MOSCOW)
    tomorrow = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    counts = {row['status']: row['count'] for row in store.db.execute(
        'SELECT status, COUNT(*) AS count FROM tracked_vacancies GROUP BY status')}
    blocks = ['<b>📋 План поиска · ' + local.strftime('%d.%m.%Y') + ' МСК</b>']
    statuses = [f'{label}: {counts[status]}' for status, label in STATUSES.items()
                if status != 'ignored' and counts.get(status)]
    blocks.append('Текущие статусы\n' + (' · '.join(statuses) or 'Пока нет сохранённых откликов. Найти вакансии: /jobs'))
    reminder_count, reminders = _preview(store, '''SELECT r.ref, r.text, r.due_at, v.payload
        FROM reminders r LEFT JOIN tracked_vacancies t ON t.ref=r.ref
        LEFT JOIN vacancies v ON v.source=t.source AND v.external_id=t.external_id
        WHERE r.delivered_at IS NULL AND r.due_at<? AND COALESCE(t.status, '')!='ignored' ''',
        'r.due_at, r.ref', (tomorrow.astimezone(timezone.utc).isoformat(),))
    blocks.append(f'<b>⏰ Напоминания на сегодня и просроченные · {reminder_count}</b>')
    for row in reminders:
        due = datetime.fromisoformat(row['due_at'])
        marker = 'Просрочено' if due < current else 'Сегодня'
        date = due.astimezone(MOSCOW).strftime('%d.%m %H:%M')
        blocks.append(f'{marker} · {date} МСК · {_title(row)}\n{escape(row["text"][:100])}\n<code>/application {row["ref"]}</code>')
    if not reminders:
        blocks.append('Нет ожидающих напоминаний на сегодня.')
    if reminder_count > VISIBLE_ITEMS:
        blocks.append('Все ожидающие напоминания: /reminders')
    action_count, actions = _preview(store, '''SELECT t.ref, d.next_action, v.payload FROM tracked_vacancies t
        JOIN career_details d ON d.ref=t.ref
        LEFT JOIN vacancies v ON v.source=t.source AND v.external_id=t.external_id
        WHERE t.status IN ('saved', 'applied', 'interview', 'offer') AND TRIM(d.next_action)!='' ''',
        't.updated_at, t.ref')
    blocks.append(f'<b>📝 Записанные следующие действия · {action_count}</b>')
    for row in actions:
        blocks.append(_title(row) + '\n' + escape(row['next_action'][:100]) + f'\n<code>/application {row["ref"]}</code>')
    if not actions:
        blocks.append('Добавьте следующий шаг кнопкой в /application ID.')
    if action_count > VISIBLE_ITEMS:
        blocks.append('Остальные карточки: /track')
    stale_count, stale = _preview(store, '''SELECT t.ref, t.updated_at, v.payload FROM tracked_vacancies t
        LEFT JOIN vacancies v ON v.source=t.source AND v.external_id=t.external_id
        WHERE t.status='applied' AND t.updated_at<=?''', 't.updated_at, t.ref',
        ((current.astimezone(timezone.utc) - timedelta(days=7)).isoformat(),))
    blocks.append(f'<b>📌 «Откликнулся»: отметка статуса от 7 дней назад · {stale_count}</b>')
    for row in stale:
        date = datetime.fromisoformat(row['updated_at']).astimezone(MOSCOW).strftime('%d.%m.%Y')
        blocks.append(_title(row) + f' · отметка {date}\n<code>/application {row["ref"]}</code>')
    if stale:
        blocks.append('Проверьте свои записи и обновите статус, если ситуация изменилась. Все отклики: /track')
    blocks.append('План составлен из ваших записей. Кнопки карточки: /application ID · Все напоминания: /reminders')
    return blocks


def daily_messages(store, current=None):
    messages, chunk = [], ''
    for block in daily_blocks(store, current):
        if chunk and length(chunk + '\n\n' + block) > 3300:
            messages.append(chunk)
            chunk = '<b>📋 План поиска · продолжение</b>'
        chunk += ('\n\n' if chunk else '') + block
    if chunk:
        messages.append(chunk)
    return messages


def handle_daily(telegram, store, update):
    if telegram.command(update) != '/today':
        return False
    from .interface import HOME_KEYBOARD
    messages = daily_messages(store)
    for text in messages[:-1]:
        telegram.send(text)
    telegram.send(messages[-1], reply_markup=HOME_KEYBOARD)
    return True
