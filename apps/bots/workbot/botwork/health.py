from datetime import datetime, timezone
from html import escape
import time
from .reporting import NAMES


def notifications(store, config, at=None):
    settings = config['alerts']
    if not settings['enabled']:
        return []
    at = at or datetime.now(timezone.utc)
    enabled = {name for name, item in config['sources'].items() if item.get('enabled')}
    result = []
    for row in store.status():
        if row['source'] not in enabled:
            continue
        name = escape(NAMES.get(row['source'], row['source']))
        failed = row['failure_count'] > 0
        if failed and row['incident'] > row['alerted_incident']:
            if row['failure_count'] < settings['failure_threshold'] or not row['failure_since']:
                continue
            elapsed = (at - datetime.fromisoformat(row['failure_since'])).total_seconds()
            if elapsed < settings['min_failure_seconds']:
                continue
            text = (f'⚠️ <b>Не удаётся проверить {name}</b>\n'
                    f'Неудачных проверок подряд: {row["failure_count"]}\n'
                    f'Сбой с (UTC): {escape(row["failure_since"])}\n'
                    f'Последний успех (UTC): {escape(row["last_success"] or "ещё не было")}\n'
                    f'Ошибка: {escape(str(row["error"])[:300])}\n'
                    'Бот продолжит проверки. Уведомления этого источника удерживаются до успешного сбора.')
            result.append({'source': row['source'], 'kind': 'failure', 'incident': row['incident'], 'text': text})
        elif not failed and row['alerted_incident'] > row['recovered_incident']:
            text = (f'✅ <b>Проверка {name} восстановилась</b>\n'
                    f'Найдено: {row["found"] or 0}, подходит: {row["matched"] or 0}\n'
                    f'Последний успех (UTC): {escape(row["last_success"] or "ещё не было")}')
            result.append({'source': row['source'], 'kind': 'recovery',
                           'incident': row['alerted_incident'], 'text': text})
    return result


def deliver_health(store, telegram, config, at=None, delay=1.1):
    sent = 0
    for item in notifications(store, config, at):
        telegram.send(item['text'])
        # Keep the incident pending until Telegram confirms delivery.
        store.acknowledge_health(item['source'], item['kind'], item['incident'])
        sent += 1
        time.sleep(delay)
    return sent
