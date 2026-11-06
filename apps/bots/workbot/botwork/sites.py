"""Owner's job-search bookmarks, independent of connected monitoring sources."""

from html import escape
import json
import os
import ipaddress
import re
from pathlib import Path
from urllib.parse import urlsplit
from .models import normalize

HEADER = '<b>Карьерный каталог</b>\nВакансии, мероприятия и материалы для ручного поиска; мониторинг — /sources.'
LIMIT = 3500  # Raw HTML in UTF-16 units; conservative for Telegram's 4096 limit.


def length(text):
    return len(text.encode('utf-16-le')) // 2


def load_sites(path=None):
    path = path or os.environ.get('BOTWORK_SITES') or Path(__file__).with_name('sites.json')
    try:
        raw = Path(path).read_bytes()
        if len(raw) > 1_000_000:
            raise ValueError
        catalog = json.loads(raw)
    except (OSError, ValueError, UnicodeError):
        raise ValueError('Не удалось прочитать каталог ссылок; проверьте BOTWORK_SITES и JSON') from None
    return validate_catalog(catalog)


def validate_catalog(catalog, max_sections=30):
    if not isinstance(catalog, list) or not 1 <= len(catalog) <= max_sections:
        raise ValueError(f'Каталог должен содержать от 1 до {max_sections} разделов')
    for section in catalog:
        if not isinstance(section, dict) or not isinstance(section.get('title'), str) or not 1 <= len(section['title']) <= 150:
            raise ValueError('У раздела каталога отсутствует заголовок')
        if not isinstance(section.get('items'), list) or not 1 <= len(section['items']) <= 1000:
            raise ValueError('В разделе каталога должны быть ссылки или заметки')
        for item in section['items']:
            if not isinstance(item, dict) or not isinstance(item.get('name'), str) or not 1 <= len(item['name']) <= 200:
                raise ValueError('У ссылки отсутствует название')
            if not isinstance(item.get('note', ''), str) or len(item.get('note', '')) > 500:
                raise ValueError('Некорректное примечание ссылки')
            url = item.get('url')
            if url is not None:
                if not isinstance(url, str) or len(url) > 800:
                    raise ValueError('Некорректный адрес ссылки')
                try:
                    parts = urlsplit(url)
                    _ = parts.port
                except ValueError:
                    raise ValueError('Некорректный адрес ссылки') from None
                if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password or any(c.isspace() or ord(c) < 32 for c in url):
                    raise ValueError('Ссылки должны использовать HTTPS без данных входа')
                try:
                    host = parts.hostname.rstrip('.')
                    if ':' in host or re.fullmatch(r'[0-9.]+', host):
                        ipaddress.ip_address(host)
                    else:
                        ascii_host = host.encode('idna').decode('ascii')
                        if len(ascii_host) > 253 or any(not re.fullmatch(r'[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?', part) for part in ascii_host.split('.')):
                            raise ValueError
                except (ValueError, UnicodeError):
                    raise ValueError('В ссылке некорректное доменное имя или IP-адрес') from None
            elif not item.get('note'):
                raise ValueError('Задайте адрес ссылки или текст заметки')
    return catalog


def filter_sites(catalog, query=''):
    if not isinstance(query, str) or len(query) > 200:
        raise ValueError('Поисковый запрос должен быть не длиннее 200 символов')
    words = normalize(query).split()
    result = []
    for section in catalog:
        items = [item for item in section['items'] if all(word in normalize(' '.join([
            section['title'], item['name'], item.get('url') or '', item.get('note') or '', item.get('id', '')])) for word in words)]
        if items:
            result.append(section | {'items': items})
    return result


def sites_messages(catalog=None, query='', hidden=False):
    catalog = load_sites() if catalog is None else catalog
    catalog = filter_sites(catalog, query)
    header = ('<b>Удалённые ссылки</b>\nВосстановить: /site_restore ID' if hidden else HEADER)
    header += '\nПоиск: ' + escape(query) if query else ''
    if not catalog:
        empty = 'Ничего не найдено. Все ссылки: /sites' if query else 'Удалённых ссылок нет.' if hidden else 'Каталог пуст. Добавить: /site_add URL название'
        return [header + '\n' + empty]
    messages, current = [], header
    for section in catalog:
        heading = '\n\n<b>' + escape(section['title']) + '</b>'
        if length(current + heading) > LIMIT:
            messages.append(current)
            current = header
        current += heading
        for item in section['items']:
            title = escape(item['name'])
            link = '<a href="' + escape(item['url'], quote=True) + '">' + title + '</a>' if item.get('url') else title
            identifier = '<code>' + escape(item['id']) + '</code> ' if item.get('id') else ''
            line = '\n• ' + identifier + link + (' — ' + escape(item['note']) if item.get('note') else '')
            if length(header + heading + line) > LIMIT:
                raise ValueError('Ссылка слишком длинная для сообщения Telegram; сократите запись')
            if length(current + line) > LIMIT:
                messages.append(current)
                current = header + heading
            current += line
    if current:
        messages.append(current)
    return messages


def send_sites(telegram, store=None, query='', hidden=False):
    try:
        from .bookmarks import BookmarkCatalog, catalog_view
        catalog = (BookmarkCatalog(store).hidden() if hidden else BookmarkCatalog(store).view()) if store else ([] if hidden else catalog_view(load_sites()))
        messages = sites_messages(catalog, query, hidden)
    except ValueError:
        if len(query) > 200:
            telegram.send('Поисковый запрос должен быть не длиннее 200 символов.')
            return
        telegram.send('Не удалось прочитать каталог ссылок. Проверьте BOTWORK_SITES и формат JSON.')
        return
    for message in messages:
        telegram.send(message)


def handle_sites(telegram, store, update):
    command = telegram.command(update)
    if command not in ('/sites', '/site_hidden', '/site_add', '/site_del', '/site_restore', '/site_rename', '/site_note', '/site_export', '/site_import', '/site_import_confirm'):
        return False
    arguments = telegram.arguments(update)
    if command in ('/sites', '/site_hidden'):
        send_sites(telegram, store, arguments, hidden=command == '/site_hidden')
        return True
    from .bookmarks import BookmarkCatalog
    import sqlite3
    try:
        catalog = BookmarkCatalog(store)
        if command == '/site_export':
            if arguments not in ('', 'all', 'added'):
                raise ValueError('Используйте /site_export или /site_export added.')
            view = catalog.view()
            if arguments == 'added':
                from .bookmarks import catalog_view
                base_ids = {item['id'] for section in catalog_view(load_sites()) for item in section['items']}
                view = [section | {'items': [item for item in section['items'] if item['id'] not in base_ids]} for section in view]
                view = [section for section in view if section['items']]
            telegram.send_json('botwork-sites-added.json' if arguments == 'added' else 'botwork-sites.json', view)
            return True
        if command == '/site_import' and update.get('message', {}).get('document'):
            arguments = json.dumps(telegram.read_json(update['message']['document']), ensure_ascii=False)
        response = catalog.execute(command, arguments, update.get('update_id'))
    except ValueError as exc:
        response = str(exc)
    except sqlite3.Error:
        response = 'Не удалось сохранить каталог. Попробуйте повторить команду позже.'
    telegram.send(escape(response))
    return True
