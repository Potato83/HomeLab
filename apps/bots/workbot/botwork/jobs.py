"""Read-only browsing of the last complete source snapshots."""
import json
from html import escape
import re
import secrets
import shlex

from .directions import LABELS, matches
from .models import Vacancy, normalize
from .matching import decide, entry_level
from .profile_matching import configured_profiles
from .pipeline import feedback_buttons, vacancy_ref

PAGE_SIZE = 5


def parse_search(arguments):
    if len(arguments) > 300:
        raise ValueError('Поиск — до 300 символов.')
    options = {'page': 1, 'source': '', 'direction': '', 'level': '', 'format': '', 'profile': '', 'query': []}
    try:
        words = shlex.split(arguments)
    except ValueError:
        raise ValueError('Закройте кавычки. Пример: /jobs profile="ИБ СПб"') from None
    for word in words:
        key, separator, value = word.partition('=')
        if separator and key in ('page', 'source', 'direction', 'level', 'format', 'profile'):
            if key == 'page':
                if not value.isdigit() or not 1 <= int(value) <= 100000:
                    raise ValueError('Страница: page=1, page=2…')
                value = int(value)
            elif key == 'direction' and value not in LABELS:
                raise ValueError('Направления: security, devsecops, devops, mlops.')
            elif key == 'source' and not re.fullmatch('[a-z_]+', value):
                raise ValueError('Источник указывается кодом из /sources.')
            elif key == 'level' and value not in ('intern', 'junior'):
                raise ValueError('Уровень: level=intern или level=junior.')
            elif key == 'format' and value not in ('remote', 'office', 'hybrid', 'unknown'):
                raise ValueError('Формат: remote, office, hybrid, unknown.')
            elif key == 'profile' and not 1 <= len(value) <= 32:
                raise ValueError('Укажите имя профиля из /profiles.')
            options[key] = value
        elif separator:
            raise ValueError('Фильтры: source, direction, level, format, profile, page.')
        else:
            options['query'].append(normalize(word))
    return options


def search_jobs(store, config, options):
    source = options['source']
    if source and source not in config['sources']:
        raise ValueError('Неизвестный источник. Список: /sources')
    profile = None
    if options.get('profile'):
        profile = next((p for p in configured_profiles(config) if p['name'].casefold() == options['profile'].casefold()), None)
        if profile is None:
            raise ValueError('Такого профиля нет. Список: /profiles')
    records = store.db.execute('''SELECT v.* FROM vacancies v WHERE v.active=1 AND v.matched=1
        AND NOT EXISTS (SELECT 1 FROM tracked_vacancies t WHERE t.source=v.source AND t.external_id=v.external_id AND t.status='ignored')
        ORDER BY v.first_seen DESC, v.source, v.external_id''')
    result = []
    for row in records:
        v = json.loads(row['payload'])['vacancy']
        if v.get('kind', 'vacancy') != 'vacancy' or not config['sources'].get(v['source'], {}).get('enabled'):
            continue
        if source and v['source'] != source:
            continue
        if options.get('level') and entry_level(v['title'], v.get('level')) != options['level']:
            continue
        if options.get('format') and v.get('work_format', 'unknown') != options['format']:
            continue
        if profile and not decide(Vacancy(**v), profile['filters'], config['sources'][v['source']].get('notify_unverified', False)).matched:
            continue
        searchable_text = normalize(' '.join((
            v.get('title', ''),
            v.get('company', ''),
            *v.get('categories', []),
            v.get('description', ''),
        )))
        if not all(word in searchable_text for word in options['query']):
            continue
        if options['direction'] and not matches(' '.join([v['title'], *v.get('categories', [])]), config['filters'] | {'directions': [options['direction']]}):
            continue
        result.append(v)
    return result


def jobs_page(store, config, options, token):
    rows = search_jobs(store, config, options)
    pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(options['page'], pages)
    lines = [f'<b>Найденные вакансии · {len(rows)}</b>', f'Страница {page}/{pages}. Из последнего сохранённого сбора; проверьте условия в источнике.']
    filters = [f'{key}={options[key]}' for key in ('source', 'direction', 'level', 'format', 'profile') if options.get(key)]
    if filters:
        lines.append('Фильтры: ' + escape(' · '.join(filters)))
    buttons = []
    for v in rows[(page - 1) * PAGE_SIZE:page * PAGE_SIZE]:
        ref = vacancy_ref(v['source'], v['external_id'])
        from .telegram import FORMATS
        lines.append(f'<b>{escape(v["title"][:140])}</b>\n{escape(v["company"][:80])} · {escape(v["source"])}\n'
                     f'{FORMATS.get(v.get("work_format"), "Не указан")} · {escape(v.get("salary", "не указана")[:80])}\n'
                     f'<a href="{escape(v["url"], quote=True)}">Источник</a> · <code>/job {ref}</code>')
        buttons.append([{'text': 'Подробнее', 'callback_data': f'job:{ref}:1'}, *feedback_buttons(v)[0]])
    if not rows:
        lines.append('Пока нет совпадений. Попробуйте другой запрос или /check.')
    navigation = []
    if page > 1:
        navigation.append({'text': '← Назад', 'callback_data': f'jobs:{token}:{page - 1}'})
    if page < pages:
        navigation.append({'text': 'Дальше →', 'callback_data': f'jobs:{token}:{page + 1}'})
    if navigation:
        buttons.append(navigation)
    lines.append('Пример: <code>/jobs SOC source=mts direction=security</code>')
    return '\n\n'.join(lines), {'inline_keyboard': buttons}


def handle_jobs(telegram, monitor, update):
    from .job_cards import handle_job_card
    if handle_job_card(telegram, monitor, update):
        return True
    callback = telegram.owner_callback(update)
    options = None
    if callback and isinstance(callback.get('data'), str) and callback['data'].startswith('jobs:'):
        match = re.fullmatch(r'jobs:([0-9a-f]{12}):([0-9]{1,6})', callback['data'])
        saved = json.loads(monitor.store.get_state('jobs_view', '{}'))
        if not match or saved.get('token') != match[1]:
            telegram.answer_callback(callback['id'], 'Этот поиск устарел. Откройте /jobs заново.')
            return True
        options, token = saved['options'], saved['token']
        options['page'] = max(1, int(match[2]))
        telegram.answer_callback(callback['id'], 'Открываю страницу')
    elif telegram.command(update) == '/jobs':
        try:
            options = parse_search(telegram.arguments(update))
        except ValueError as exc:
            telegram.send(escape(str(exc)))
            return True
        token = secrets.token_hex(6)
        monitor.store.set_state('jobs_view', json.dumps({'token': token, 'options': options}))
    else:
        return False
    try:
        text, markup = jobs_page(monitor.store, monitor.config, options, token)
        telegram.send(text, reply_markup=markup)
    except ValueError as exc:
        telegram.send(escape(str(exc)))
    return True
