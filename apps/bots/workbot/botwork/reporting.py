from html import escape
from . import __version__

NAMES = {"yandex": "Яндекс", "kaspersky": "Kaspersky", "bastion": "Бастион",
         "croc": "КРОК", "mts": "МТС", "x5_internships": "X5 — стажировки",
         "pt_start_training": "PT Start — стажировка с обучением",
         "pt_fast_track": "PT Start — Fast Track", "sber": "Сбер", "hh": "hh.ru",
         "habr": "Хабр Карьера", "yandex_jobs": "Яндекс — вакансии",
         'ctftime': 'CTFtime — мероприятия', 'ods_events': 'ODS — мероприятия', 'croc_internships': 'КРОК — программа стажировок'}


def source_views(store, config=None):
    from .profile_matching import combined_collection_filters
    previous = {row['source']: row for row in store.status()}
    settings = config['sources'] if config else {name: {'enabled': True} for name in previous}
    enabled = [name for name, item in settings.items() if item.get('enabled')]
    counts = store.pending_counts(enabled)
    result = []
    for name, item in settings.items():
        old = previous.get(name, {})
        is_enabled = bool(item.get('enabled')) and (name not in ('ctftime', 'ods_events') or not config or combined_collection_filters(config).get('events_enabled', False))
        state = ('disabled' if not is_enabled else 'unchecked' if not old.get('last_attempt')
                 else 'error' if old.get('error') is not None else 'ready')
        result.append(old | {'source': name, 'name': NAMES.get(name, name), 'enabled': is_enabled,
            'url': item.get('url'), 'state': state, 'pending': counts.get(name, 0),
            'last_attempt': old.get('last_attempt'), 'last_success': old.get('last_success'),
            'error': old.get('error'), 'failure_count': old.get('failure_count', 0),
            'unverified': int(store.get_state('unverified:' + name, '0')),
            'watch_errors': int(store.get_state('event_watch_errors:' + name, '0'))})
    return result


def status_text(store, config=None):
    rows = source_views(store, config)
    enabled = [row for row in rows if row['enabled']]
    pending = sum(row['pending'] for row in enabled)
    held = sum(row['pending'] for row in enabled if row['state'] != 'ready')
    lines = [f'<b>BotWork {__version__}</b>', f'Включено источников: {len(enabled)}',
             f'Очередь вакансий и событий: {pending} (из них удерживается из-за ошибок: {held})']
    if config and config['monitor'].get('paused'):
        lines.append('⏸ Автоматический мониторинг и уведомления на паузе')
    if store.get_state('last_scan_error'):
        lines.append('Ошибка фоновой проверки: ' + escape(store.get_state('last_scan_error')))
    if store.get_state('last_reminder_error'):
        lines.append('Напоминание ожидает повтора: ' + escape(store.get_state('last_reminder_error')[:180]))
    if config and config['monitor'].get('delivery_mode') == 'digest':
        lines.append(f'Подборка раз в {config["monitor"].get("digest_seconds", 3600) // 60} мин.')
    delivery_error = store.get_state('last_delivery_error')
    if delivery_error:
        lines.append('Последняя ошибка отправки: ' + escape(delivery_error[:180]))
        lines.append('Время (UTC): ' + escape(store.get_state('last_delivery_error_at', 'неизвестно')))
    if not rows:
        return '\n'.join(lines + ['Проверок пока нет.'])
    for row in rows:
        label = escape(row['name'])
        if row['state'] == 'disabled':
            lines.append(f'⏸ {label}: отключён')
        elif row['state'] == 'unchecked':
            lines.append(f'◻️ {label}: проверок пока нет')
        elif row['state'] == 'error':
            lines.append(f'⚠️ {label}: ошибок подряд {row["failure_count"]}')
            lines.append('Ошибка: ' + escape(str(row['error'])[:180]))
            lines.append('Последний успех (UTC): ' + escape(row['last_success'] or 'ещё не было'))
        else:
            icon = '🟡' if row['unverified'] or row['watch_errors'] else '✅'
            lines.append(f'{icon} {label}: найдено {row.get("found") or 0}, подходит {row.get("matched") or 0}')
            if row['unverified']:
                label = 'Регистрация не подтверждена у событий' if row['source'] in ('ctftime', 'ods_events') else 'Набор не подтверждён у направлений'
                lines.append(f'{label}: {row["unverified"]}')
            if row['watch_errors']:
                lines.append(f'Не удалось перепроверить ранее присланных событий: {row["watch_errors"]}; отмена не предполагается')
            lines.append('Последний успех (UTC): ' + escape(row['last_success'] or 'ещё не было'))
    return '\n'.join(lines)


def sources_text(store, config):
    lines = ['<b>Источники вакансий и мероприятий</b>']
    labels = {'disabled': 'отключён', 'unchecked': 'ещё не проверен', 'error': 'ошибка', 'ready': 'работает'}
    for row in source_views(store, config):
        name = escape(row['name'])
        link = f'<a href="{escape(row["url"], quote=True)}">{name}</a>' if row['url'] else name
        suffix = '; есть неподтверждённый набор' if row['state'] == 'ready' and row['unverified'] else ''
        period = config['sources'][row['source']].get('interval_seconds', config['monitor']['interval_seconds']) // 60
        lines.append(f'{link} (<code>{escape(row["source"])}</code>): {labels[row["state"]]}{suffix}; {period} мин.')
    return '\n'.join(lines)
