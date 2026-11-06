"""Read stored vacancy details without fetching or marking notifications sent."""
from html import escape
import json
import re

from .career import send_application
from .matching import entry_level
from .pipeline import feedback_buttons
from .sites import length
from .telegram import FORMATS


def description_pages(text, limit=2800):
    """Split plain text by escaped UTF-16 size, preserving every character."""
    pages, chunk, size = [], [], 0
    for char in text:
        cost = {'&': 5, '<': 4, '>': 4}.get(char, 2 if ord(char) > 0xFFFF else 1)
        if size + cost > limit and chunk:
            pages.append(''.join(chunk))
            chunk, size = [], 0
        chunk.append(char)
        size += cost
    if chunk:
        pages.append(''.join(chunk))
    return pages or ['Источник не передал отдельное описание. Условия доступны по ссылке вакансии.']


def vacancy_sections(vacancy, row, payload, config, status):
    v, d = vacancy, payload.get('decision', {})
    active = bool(row['active']) and v.get('active', True) and v.get('recruitment_status') != 'closed'
    recruitment = 'Неактивна в сохранённом снимке' if not active else (
        'Набор не подтверждён' if v.get('recruitment_status') == 'unknown' else 'Была открыта при последнем сборе')
    sections = ['<b>' + escape(v['title'][:200]) + '</b>\n' + escape(v['company'][:120]),
                '<b>' + recruitment + '</b>',
                'Город: ' + escape(', '.join(v.get('cities', []))[:200] or 'не указан'),
                'Формат: ' + FORMATS.get(v.get('work_format'), 'Не указан'),
                'Зарплата: ' + escape(v.get('salary', 'не указана')[:150])]
    if d.get('matched'):
        label = 'Стажировка' if entry_level(v['title'], v.get('level')) == 'intern' else 'Junior'
        sections.append('Уровень: ' + label)
    if v.get('experience'):
        sections.append('Опыт: ' + escape(v['experience'][:200]))
    if v.get('work_format_label'):
        sections.append('Условия: ' + escape(v['work_format_label'][:200]))
    if v.get('employment_known'):
        from .employment import LABELS
        sections.append('Занятость: ' + ', '.join(LABELS[key] for key in v.get('employment_types', []) if key in LABELS))
    if d.get('matching_profiles'):
        sections.append('Совпавшие профили: ' + ', '.join(escape(name[:32]) for name in d['matching_profiles'][:10]))
    for note in v.get('notes', [])[:3]:
        sections.append(escape(note[:250]))
    for warning in d.get('warnings', [])[:5]:
        sections.append('⚠️ ' + escape(warning[:250]))
    if not config['sources'].get(v['source'], {}).get('enabled'):
        sections.append('Источник сейчас выключен в настройках.')
    if status and status['error']:
        sections.append('Последняя проверка источника завершилась ошибкой; показана прежняя сохранённая карточка.')
    sections.append(f'<a href="{escape(v["url"], quote=True)}">Открыть источник</a>')
    if v.get('application_url'):
        sections.append(f'<a href="{escape(v["application_url"], quote=True)}">Заявка / направление</a>')
    sections.append('Сведения из сохранённой базы. Перед откликом проверьте текущие условия в источнике.')
    return sections


def send_job(telegram, monitor, ref, page=1):
    store = monitor.store
    vacancy = store.vacancy_by_ref(ref)
    if not vacancy:
        raise ValueError('Вакансия не найдена. ID указан в /jobs и уведомлениях.')
    row = store.db.execute('SELECT * FROM vacancies WHERE source=? AND external_id=?', (vacancy['source'], vacancy['external_id'])).fetchone()
    payload = json.loads(row['payload'])
    pages = description_pages(vacancy.get('description', ''))
    page = min(max(1, page), len(pages))
    if page == 1:
        status = store.db.execute('SELECT error FROM source_status WHERE source=?', (vacancy['source'],)).fetchone()
        chunk = ''
        for block in vacancy_sections(vacancy, row, payload, monitor.config, status):
            if chunk and length(chunk + '\n' + block) > 3300:
                telegram.send(chunk)
                chunk = ''
            chunk += ('\n' if chunk else '') + block
        telegram.send(chunk)
    buttons = feedback_buttons(vacancy)
    buttons.append([{'text': '📝 Мои записи и напоминание', 'callback_data': 'job:app:' + ref}])
    navigation = []
    if page > 1:
        navigation.append({'text': '← Описание', 'callback_data': f'job:{ref}:{page - 1}'})
    if page < len(pages):
        navigation.append({'text': 'Описание →', 'callback_data': f'job:{ref}:{page + 1}'})
    if navigation:
        buttons.append(navigation)
    text = f'<b>Описание · {page}/{len(pages)}</b>\n\n' + escape(pages[page - 1], quote=False)
    text += f'\n\n<code>/application {ref}</code>'
    telegram.send(text, reply_markup={'inline_keyboard': buttons})


def handle_job_card(telegram, monitor, update):
    callback = telegram.owner_callback(update)
    try:
        if callback and isinstance(callback.get('data'), str) and callback['data'].startswith('job:'):
            match = re.fullmatch(r'job:(?:([0-9a-f]{8}):([0-9]{1,6})|app:([0-9a-f]{8}))', callback['data'])
            if not match:
                telegram.answer_callback(callback['id'], 'Неизвестная карточка')
                return True
            telegram.answer_callback(callback['id'], 'Открываю карточку')
            if match[3]:
                send_application(telegram, monitor.store, match[3])
            else:
                send_job(telegram, monitor, match[1], int(match[2]))
        elif telegram.command(update) == '/job':
            args = telegram.arguments(update).split()
            if len(args) not in (1, 2) or (len(args) == 2 and not re.fullmatch(r'page=[0-9]{1,6}', args[1])):
                raise ValueError('Команда: /job ID или /job ID page=2')
            send_job(telegram, monitor, args[0], int(args[1][5:]) if len(args) == 2 else 1)
        else:
            return False
    except ValueError as exc:
        telegram.send(escape(str(exc)))
    return True
