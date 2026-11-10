from html import escape
import json
import re
import time
from urllib.parse import urlsplit
from .dedup import notification_key
from .http import telegram_call
from .matching import decide
from .models import Vacancy

BOT_COMMANDS = [{'command': name, 'description': label} for name, label in [
    ('jobs', 'Вакансии по ключевым словам'), ('events', 'Ближайшие события'),
    ('keywords', 'Посмотреть или изменить слова'), ('check', 'Обновить источники'),
    ('status', 'Состояние источников'), ('help', 'Справка')]]

def private_chat_id(value):
    text = str(value) if value is not None else ''
    if not re.fullmatch(r'[1-9][0-9]*', text):
        raise ValueError('OWNER_ID должен быть ID личного чата')
    return text

def format_message(payload):
    v, d = payload['vacancy'], payload['decision']
    lines = ['<b>' + escape(v['title'][:240]) + '</b>', escape(v['company'][:120])]
    if v.get('kind') == 'event':
        from .events import instant
        from datetime import timezone, timedelta
        date = instant(v['start_at']).astimezone(timezone(timedelta(hours=3))).strftime('%d.%m.%Y %H:%M МСК')
        lines.append(date)
    else:
        lines.append(escape(v.get('salary', 'не указана')[:100]))
    if v.get('work_format_label'):
        lines.append(escape(v['work_format_label'][:200]))
    if v.get('cities'):
        lines.append(escape(', '.join(v['cities'])[:180]))
    lines.append('Ключевые слова: ' + escape(d.get('reason', '')[:200]))
    lines.extend(escape(n[:250]) for n in v.get('notes', [])[:2])
    lines.extend('⚠ ' + escape(w[:200]) for w in d.get('warnings', [])[:2])
    url = v['url']
    parts = urlsplit(url)
    if parts.scheme == 'https' and parts.hostname and not parts.username and not parts.password:
        lines.append('<a href="' + escape(url, quote=True) + '">Открыть · ' + escape(v['source']) + '</a>')
    return '\n'.join(lines)

class Telegram:
    def __init__(self, token, chat_id):
        if not token:
            raise ValueError('Заполните BOT_TOKEN в .env')
        self.token, self.chat_id = token, private_chat_id(chat_id)

    def send(self, message, reply_markup=None):
        payload = {'chat_id': self.chat_id, 'text': message, 'parse_mode': 'HTML',
                   'link_preview_options': {'is_disabled': True}}
        if reply_markup:
            payload['reply_markup'] = reply_markup
        return telegram_call(self.token, 'sendMessage', payload)

    def set_commands(self):
        return telegram_call(self.token, 'setMyCommands', {'commands': BOT_COMMANDS,
                            'scope': {'type': 'chat', 'chat_id': int(self.chat_id)}})

    def updates(self, offset):
        return telegram_call(self.token, 'getUpdates', {'offset': offset, 'timeout': 20,
                            'allowed_updates': ['message']}, timeout=30)

    def command_text(self, update):
        if not isinstance(update, dict) or not isinstance(update.get('message'), dict):
            return None
        message = update['message']
        chat, sender = message.get('chat'), message.get('from')
        if (not isinstance(chat, dict) or not isinstance(sender, dict)
                or str(chat.get('id')) != self.chat_id or str(sender.get('id')) != self.chat_id
                or chat.get('type') != 'private'):
            return None
        text = message.get('text')
        return text.strip() if isinstance(text, str) and text.strip().startswith('/') else None

    def command(self, update):
        text = self.command_text(update)
        return text.split(maxsplit=1)[0].split('@', 1)[0].lower() if text else None

    def arguments(self, update):
        text = self.command_text(update)
        parts = text.split(maxsplit=1) if text else []
        return parts[1].strip() if len(parts) == 2 else ''

def deliver(store, telegram, enabled_sources, limit, delay=0.2, rules=None):
    sent = {}
    for source, external_id, v in store.sent_vacancies():
        key = notification_key(v)
        if key:
            sent.setdefault(key, set()).add(source)
    count = 0
    for row in store.pending(enabled_sources, limit):
        payload = json.loads(row['payload'])
        v = payload['vacancy']
        if rules is not None and not decide(Vacancy(**v), rules).matched:
            store.suppress(row)
            continue  # An event may expire while Telegram is unavailable.
        key = notification_key(v)
        if key and any(source != row['source'] for source in sent.get(key, set())):
            store.mark_sent(row)
            continue
        telegram.send(format_message(payload))
        store.mark_sent(row)  # Never consume a failed delivery.
        if key:
            sent.setdefault(key, set()).add(row['source'])
        count += 1
        time.sleep(delay)
    return count
