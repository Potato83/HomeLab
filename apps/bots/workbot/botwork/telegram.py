from html import escape
import json
import re
from .dedup import notification_key
from .http import telegram_call

FORMATS = {"remote": "Удалённо", "office": "Офис", "hybrid": "Гибрид", "field": "Разъездная работа", "unknown": "Не указан"}

BOT_COMMANDS = [
    {"command": "start", "description": "Открыть главное меню"},
    {"command": "menu", "description": "Открыть главное меню"},
    {"command": "help", "description": "Список команд"},
    {"command": "check", "description": "Искать сейчас"},
    {"command": "status", "description": "Состояние поиска и очереди"},
    {"command": "today", "description": "План на сегодня"},
    {"command": "jobs", "description": "Искать вакансии"},
    {"command": "job", "description": "Открыть вакансию по ID"},
    {"command": "track", "description": "Мои вакансии и отклики"},
    {"command": "application", "description": "Карточка отклика"},
    {"command": "remind", "description": "Напомнить по отклику"},
    {"command": "reminders", "description": "Список напоминаний"},
    {"command": "settings", "description": "Настройки поиска"},
    {"command": "profiles", "description": "Профили поиска"},
    {"command": "profile", "description": "Выбрать или изменить профиль"},
    {"command": "sites", "description": "Каталог полезных ссылок"},
    {"command": "site_add", "description": "Добавить ссылку в каталог"},
    {"command": "insights", "description": "Сводка по откликам"},
    {"command": "track_export", "description": "Экспортировать отклики"},
    {"command": "sources", "description": "Список источников"},
    {"command": "source", "description": "Включить или выключить источник"},
    {"command": "set", "description": "Изменить параметр поиска"},
    {"command": "settings_reset", "description": "Сбросить настройки поиска"},
    {"command": "cancel", "description": "Отменить ожидающий ввод"},
]


def private_chat_id(value):
    text = str(value) if value is not None else ''
    if not re.fullmatch(r'[1-9][0-9]*', text):
        raise ValueError('TELEGRAM_CHAT_ID должен быть положительным числовым ID личного чата')
    return text


def format_message(payload):
    v = payload["vacancy"]
    if v.get('kind') == 'event':
        return format_event(payload)
    d = payload["decision"]
    unknown = v.get('recruitment_status', 'open') == 'unknown'
    marker = '🟡' if unknown else '🔐'
    lines = [f'{marker} <b>{escape(v["title"][:300])}</b>', escape(v["company"][:150])]
    if unknown:
        lines.append('<b>Набор не подтверждён</b>')
    elif payload.get('_notification_kind') == 'confirmation':
        lines.append('✅ <b>Набор теперь подтверждён</b>')
    lines += [
             f'Город: {escape(", ".join(v["cities"])[:200] or "не указан")}',
             f'Формат: {FORMATS[v["work_format"]]}', f'Зарплата: {escape(v["salary"][:100])}',
             f'Подходит: {escape(d["reason"])}']
    if d.get('matching_profiles'):
        labels = ', '.join(escape(name[:80]) for name in d['matching_profiles'][:10])
        lines.append('Профили: ' + labels)
    if v.get('employment_known'):
        from .employment import LABELS
        lines.append('Занятость: ' + ', '.join(LABELS[x] for x in v.get('employment_types', []) if x in LABELS))
    if v.get("experience"):
        lines.append("Опыт: " + escape(v["experience"][:100]))
    if v.get("work_format_label"):
        lines.append("Условия формата: " + escape(v["work_format_label"][:200]))
    lines += [escape(note[:500]) for note in v.get('notes', [])[:3]]
    lines += ["⚠️ " + escape(w) for w in d["warnings"]]
    lines += [f'<a href="{escape(v["url"], quote=True)}">Открыть объявление</a>', f'Источник: {escape(v["source"])}']
    if v.get('application_url'):
        lines.append(f'<a href="{escape(v["application_url"], quote=True)}">Форма заявки</a>')
    from .pipeline import vacancy_ref
    lines.append(f"Откликнулись? <code>/track {vacancy_ref(v['source'], v['external_id'])} applied</code>")
    return "\n".join(lines)


def format_event(payload):
    from .events import instant
    from datetime import timezone, timedelta
    v, d = payload['vacancy'], payload['decision']
    def date(value):
        return instant(value).astimezone(timezone(timedelta(hours=3))).strftime('%d.%m.%Y %H:%M МСК') if value else 'не указан'
    registration = {'open': 'Открыта', 'closed': 'Закрыта', 'unknown': 'Не подтверждена'}
    registration_label = 'Не актуальна: мероприятие отменено' if payload.get('_notification_kind') == 'event_cancelled' else registration[v.get('recruitment_status', 'unknown')]
    lines = ['📅 <b>' + escape(v['title'][:240]) + '</b>', escape(v['company'][:150])]
    if d.get('matching_profiles'):
        labels = ', '.join(escape(name[:80]) for name in d['matching_profiles'][:10])
        lines.append('Профили: ' + labels)
    if payload.get('_notification_kind') == 'event_cancelled':
        lines.append('⛔ <b>Мероприятие отменено</b>')
    elif payload.get('_notification_kind') == 'event_restored':
        lines.append('✅ <b>Мероприятие снова активно в календаре</b>')
    elif payload.get('_notification_kind') == 'event_update':
        lines.append('🔄 <b>Обновились сведения мероприятия</b>')
    elif payload.get('_notification_kind') == 'confirmation':
        lines.append('✅ <b>Регистрация теперь подтверждена</b>')
    lines += ['Начало: ' + date(v.get('start_at')), 'Окончание: ' + date(v.get('end_at')),
              'Тип: ' + escape(v.get('event_type', 'unknown')),
              'Город: ' + escape(', '.join(v['cities'])[:200] or 'не указан'),
              'Формат: ' + {'remote': 'Онлайн', 'office': 'Очно', 'hybrid': 'Гибрид', 'unknown': 'Нужно уточнить'}[v['work_format']],
              'Регистрация: <b>' + registration_label + '</b>',
              'Дедлайн регистрации: ' + date(v.get('registration_deadline')),
              'Язык: ' + escape(v.get('language', 'не указан')[:100]),
              'Стоимость: ' + escape(v.get('price', 'не указана')[:100]),
              'Подходит: ' + escape(d['reason'])]
    changed = payload.get('_event_changed_fields', [])
    field_labels = {'start_at': 'начало', 'end_at': 'окончание', 'registration_deadline': 'срок регистрации',
                    'cities': 'место', 'work_format': 'формат', 'language': 'язык',
                    'language_codes': 'язык', 'price': 'стоимость', 'price_kind': 'стоимость'}
    changed_labels = list(dict.fromkeys(field_labels[key] for key in changed if key in field_labels))
    if changed_labels:
        lines.append('Изменилось: ' + ', '.join(changed_labels))
    elif payload.get('_notification_kind') == 'event_update':
        lines.append('Изменились даты или срок регистрации')
    lines += [escape(note[:350]) for note in v.get('notes', [])[:3]]
    lines += ['⚠️ ' + escape(w[:300]) for w in d.get('warnings', [])]
    lines += ['<a href="' + escape(v['url'], quote=True) + '">Подробнее о мероприятии</a>']
    if v.get('application_url') and payload.get('_notification_kind') != 'event_cancelled':
        lines.append('<a href="' + escape(v['application_url'], quote=True) + '">Сайт / регистрация у организатора</a>')
    return '\n'.join(lines)


class Telegram:
    def __init__(self, token, chat_id):
        if not token or not chat_id:
            raise ValueError("Заполните TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в .env")
        self.token, self.chat_id = token, private_chat_id(chat_id)

    def send(self, message, reply_markup=None):
        payload = {"chat_id": self.chat_id, "text": message,
            "parse_mode": "HTML", "link_preview_options": {"is_disabled": True}}
        if reply_markup:
            payload['reply_markup'] = reply_markup
        return telegram_call(self.token, "sendMessage", payload)

    def set_commands(self):
        return telegram_call(self.token, "setMyCommands", {
            "commands": BOT_COMMANDS,
            "scope": {"type": "chat", "chat_id": int(self.chat_id)},
        })

    def answer_callback(self, callback_id, text):
        return telegram_call(self.token, 'answerCallbackQuery', {'callback_query_id': callback_id, 'text': text})

    def updates(self, offset):
        return telegram_call(self.token, "getUpdates", {"offset": offset, "timeout": 20,
            "allowed_updates": ["message", "callback_query"]}, timeout=30)

    def send_json(self, filename, content):
        from .documents import send_json
        return send_json(self.token, self.chat_id, filename, content)

    def send_csv(self, filename, content):
        from .documents import send_file
        return send_file(self.token, self.chat_id, filename, content, 'text/csv')

    def read_json(self, document):
        from .documents import read_json
        return read_json(self.token, document)

    def command(self, update):
        text = self.command_text(update)
        return text.split(maxsplit=1)[0].split('@', 1)[0] if text else None

    def arguments(self, update):
        text = self.command_text(update)
        parts = text.split(maxsplit=1) if text else []
        return parts[1].strip() if len(parts) == 2 else ''

    def owner_callback(self, update):
        if not isinstance(update, dict):
            return None
        callback = update.get('callback_query')
        if not isinstance(callback, dict):
            return None
        sender, message = callback.get('from'), callback.get('message')
        if not isinstance(sender, dict) or not isinstance(message, dict):
            return None
        chat = message.get('chat')
        if not isinstance(chat, dict) or str(sender.get('id')) != self.chat_id or str(chat.get('id')) != self.chat_id or chat.get('type') != 'private':
            return None
        if not isinstance(callback.get('id'), str) or not callback['id'] or type(update.get('update_id')) is not int or update['update_id'] < 0:
            return None
        return callback

    def command_text(self, update):
        if not isinstance(update, dict):
            return None
        message = update.get("message")
        if not isinstance(message, dict):
            return None
        chat, sender = message.get('chat'), message.get('from')
        # Require both the private owner chat and its owner account.
        if (not isinstance(chat, dict) or not isinstance(sender, dict)
                or str(chat.get("id")) != self.chat_id
                or str(sender.get("id")) != self.chat_id
                or chat.get("type") != "private"):
            return None
        text = message.get('text') or (message.get('caption') if message.get('document') else None)
        from .interface import NAVIGATION
        if isinstance(text, str) and text.strip() in NAVIGATION:
            return NAVIGATION[text.strip()]
        return text.strip() if isinstance(text, str) and text.strip().startswith('/') else None


def _delivery_groups(store, rows):
    """Combine only exact, well-described cross-source vacancy duplicates."""
    sent = {}
    for source, external_id, vacancy in store.sent_vacancies():
        key = notification_key(vacancy)
        if key:
            sent.setdefault(key, set()).add((source, external_id))
    groups, by_key = [], {}
    for row in rows:
        payload = json.loads(row['payload'])
        vacancy = payload.get('vacancy', {})
        key = notification_key(vacancy) if not payload.get('_notification_kind') else None
        previously_sent = sent.get(key, set()) if key else set()
        if any(record[0] != row['source'] for record in previously_sent):
            # This equivalent listing was already delivered from another site.
            store.mark_sent(row)
            continue
        if key and key in by_key:
            group = groups[by_key[key]]
            if all(existing['source'] != row['source'] for existing in group):
                group.append(row)
            else:
                groups.append([row])
            continue
        if key:
            by_key[key] = len(groups)
        groups.append([row])
    return groups


def _source_links(group):
    if len(group) < 2:
        return ''
    links = []
    for row in group[1:]:
        vacancy = json.loads(row['payload'])['vacancy']
        links.append('<a href="' + escape(vacancy['url'], quote=True) + '">' +
                     escape(vacancy.get('source', row['source'])) + '</a>')
    return '\nЕщё источники: ' + ', '.join(links)


def _mark_group_sent(store, group):
    for row in group:
        store.mark_sent(row)


def _group_payload(group):
    payload = json.loads(group[0]['payload'])
    decision = payload['decision']
    for row in group[1:]:
        other = json.loads(row['payload'])['decision']
        for field in ('matching_profiles', 'warnings'):
            decision[field] = list(dict.fromkeys([*decision.get(field, []), *other.get(field, [])]))
    return payload


def _deliver_rows(store, telegram, rows, delay):
    import time
    sent = 0
    for group in _delivery_groups(store, rows):
        payload = _group_payload(group)
        message = format_message(payload) + _source_links(group)
        from .pipeline import feedback_buttons
        buttons = feedback_buttons(payload['vacancy'])
        if buttons:
            from .pipeline import vacancy_ref
            v = payload['vacancy']
            buttons.append([{'text': '📄 Полное описание', 'callback_data': 'job:' + vacancy_ref(v['source'], v['external_id']) + ':1'}])
            telegram.send(message, reply_markup={'inline_keyboard': buttons})
        else:
            telegram.send(message)
        # Mark only confirmed deliveries. A crash between send and commit can duplicate one message.
        _mark_group_sent(store, group)
        sent += 1
        time.sleep(delay)
    return sent


def deliver(store, telegram, enabled_sources, limit, delay=1.1):
    return _deliver_rows(store, telegram, store.pending(enabled_sources, limit), delay)


def deliver_event_cancellations(store, telegram, enabled_sources, limit, delay=1.1):
    """Send confirmed event cancellations immediately, including in digest mode."""
    rows = store.pending_event_cancellations(enabled_sources, limit)
    return _deliver_rows(store, telegram, rows, delay)


def deliver_digest(store, telegram, enabled_sources, limit, delay=1.1):
    """Commit only the rows included in each accepted compact digest message."""
    import time
    from .sites import length
    pending = store.pending(enabled_sources, limit)
    groups = _delivery_groups(store, pending)
    batches, rows, text = [], [], '<b>Подборка вакансий и мероприятий</b>'
    for group in groups:
        row = group[0]
        p = _group_payload(group); v, d = p['vacancy'], p['decision']
        if v.get('kind') == 'event':
            entry = '\n\n' + format_event(p)
            if length(entry) > 3400:
                from .http import FetchError
                raise FetchError('Слишком длинная карточка мероприятия')
            if rows and length(text + entry) > 3500:
                batches.append((text, rows)); text, rows = '<b>Подборка вакансий и мероприятий — продолжение</b>', []
            text += entry; rows.append(group)
            continue
        status = '\n🟡 <b>Набор не подтверждён</b>' if v.get('recruitment_status') == 'unknown' else '\n✅ Набор теперь подтверждён' if p.get('_notification_kind') == 'confirmation' else ''
        entry = '\n\n<a href="' + escape(v['url'], quote=True) + '">' + escape(v['title'][:220]) + '</a>'
        entry += '\n' + escape(v['company'][:100]) + ' · ' + escape(', '.join(v['cities'])[:120] or 'город не указан') + ' · ' + FORMATS[v['work_format']]
        entry += status + '\nЗарплата: ' + escape(v['salary'][:100])
        if d.get('matching_profiles'):
            entry += '\nПрофили: ' + escape(', '.join(name[:60] for name in d['matching_profiles'][:10]))
        if v.get('employment_known'):
            from .employment import LABELS
            entry += '\nЗанятость: ' + ', '.join(LABELS[x] for x in v.get('employment_types', []) if x in LABELS)
        if v.get('notes'):
            entry += '\n' + escape('; '.join(v['notes'])[:350])
        if d.get('warnings'):
            entry += '\n⚠️ ' + escape('; '.join(d['warnings'])[:350])
        if v.get('application_url'):
            entry += '\n<a href="' + escape(v['application_url'], quote=True) + '">Форма заявки</a>'
        from .pipeline import vacancy_ref
        ref = vacancy_ref(v['source'], v['external_id'])
        entry += f"\n<code>/job {ref}</code> · <code>/track {ref} applied</code>"
        entry += _source_links(group)
        if length(entry) > 3400:
            from .http import FetchError
            raise FetchError('Карточка слишком длинная для подборки; проверьте источник')
        if rows and length(text + entry) > 3500:
            batches.append((text, rows)); text, rows = '<b>Подборка вакансий — продолжение</b>', []
        text += entry; rows.append(group)
    if rows:
        batches.append((text, rows))
    sent = 0
    for text, rows in batches:
        from .pipeline import feedback_buttons
        buttons = [button for group in rows for button in feedback_buttons(_group_payload(group)['vacancy'], label=True)]
        if buttons:
            telegram.send(text, reply_markup={'inline_keyboard': buttons})
        else:
            telegram.send(text)
        for group in rows:
            _mark_group_sent(store, group); sent += 1
        time.sleep(delay)
    return sent
