"""Persistent, reversible owner edits over the read-only bookmark catalogue."""

from copy import deepcopy
import hashlib
import json
import re
import secrets
import time
from urllib.parse import urlsplit, urlunsplit

from .sites import load_sites, sites_messages, validate_catalog

ADDED = 'Добавленные вами'


def identity(item):
    if item.get('url'):
        parts = urlsplit(item['url'])
        host = parts.hostname.lower()
        if ':' in host:  # Preserve brackets around an IPv6 host.
            host = '[' + host + ']'
        if parts.port and parts.port != 443:
            host += ':' + str(parts.port)
        return urlunsplit(('https', host, parts.path or '/', parts.query, parts.fragment))
    return 'note:' + item['name'] + '\n' + item.get('note', '')


def bookmark_id(item):
    return ('s' if item.get('url') else 'n') + hashlib.sha256(identity(item).encode()).hexdigest()[:12]


def catalog_view(base, overrides=(), include_hidden=False):
    catalog = deepcopy(base)
    entries, fingerprints = {}, {}
    for section in catalog:
        kept = []
        for item in section['items']:
            key, fingerprint = bookmark_id(item), identity(item)
            if key in fingerprints and fingerprints[key] != fingerprint:
                raise ValueError('Конфликт идентификаторов ссылок; проверьте каталог')
            if key in entries:
                continue
            fingerprints[key] = fingerprint
            item['id'] = key
            entries[key] = item
            kept.append(item)
        section['items'] = kept
    additions, hidden = {}, set()
    for row in overrides:
        key = row['id']
        if row['item'] is not None:
            try:
                item = json.loads(row['item'])
                validate_catalog([{'title': ADDED, 'items': [item]}])
                if bookmark_id(item) != key and not (key.startswith('n') and not item.get('url')):
                    raise ValueError
            except (ValueError, TypeError, KeyError):
                raise ValueError('Повреждена сохранённая ссылка; проверьте резервную копию базы') from None
            section_name = item.pop('_section', ADDED)
            if key in fingerprints and identity(item) != fingerprints[key] and item.get('url'):
                raise ValueError('Конфликт идентификаторов ссылок')
            item['id'] = key
            if key in entries:
                entries[key].clear()
                entries[key].update(item)
            else:
                entries[key] = item
                additions.setdefault(section_name, []).append(item)
        if row['hidden']:
            hidden.add(key)
    for title, items in additions.items():
        catalog.append({'title': title, 'items': items})
    result = []
    for section in catalog:
        items = [item for item in section['items'] if include_hidden or item['id'] not in hidden]
        if items:
            result.append(section | {'items': items})
    return result


class BookmarkCatalog:
    def __init__(self, store):
        self.db = store.db

    def view(self, include_hidden=False):
        return catalog_view(load_sites(), self.db.execute('SELECT * FROM site_overrides ORDER BY id').fetchall(), include_hidden)

    def hidden(self):
        hidden = {row['id'] for row in self.db.execute('SELECT id FROM site_overrides WHERE hidden=1')}
        result = []
        for section in self.view(include_hidden=True):
            items = [item for item in section['items'] if item['id'] in hidden]
            if items:
                result.append(section | {'items': items})
        return result

    def execute(self, command, arguments, update_id):
        if type(update_id) is not int or update_id < 0:
            raise ValueError('Не удалось определить команду Telegram. Отправьте её ещё раз.')
        # Commit the edit and its reply together. If Telegram delivery fails,
        # replaying the same update returns the original reply without editing twice.
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            receipt = self.db.execute('SELECT response FROM site_commands WHERE update_id=?', (update_id,)).fetchone()
            if receipt:
                return receipt['response']
            try:
                response = self._edit(command, arguments)
            except ValueError as exc:
                response = str(exc)
            self.db.execute('INSERT INTO site_commands VALUES (?, ?)', (update_id, response))
        return response

    def _edit(self, command, arguments):
        catalog = self.view(include_hidden=True)
        entries = {item['id']: item for section in catalog for item in section['items']}
        sections = {item['id']: section['title'] for section in catalog for item in section['items']}
        if command == '/site_import':
            if not arguments.strip():
                raise ValueError('Прикрепите JSON-файл с подписью /site_import или укажите JSON после команды. Сначала будет показан предпросмотр.')
            if len(arguments.encode('utf-8')) > 1_000_000:
                raise ValueError('Файл JSON должен быть не больше 1 МБ.')
            try:
                incoming = json.loads(arguments)
            except (ValueError, RecursionError):
                raise ValueError('Некорректный JSON каталога.') from None
            plan, counts = self.import_plan(incoming)
            token = secrets.token_hex(6)
            self.db.execute('DELETE FROM site_imports WHERE expires<?', (int(time.time()),))
            self.db.execute('INSERT INTO site_imports VALUES (?,?,?)', (token, json.dumps({
                'catalog': incoming, 'fingerprint': self.fingerprint()}, ensure_ascii=False), int(time.time()) + 3600))
            return f'Предпросмотр импорта: новых {counts[0]}, изменений {counts[1]}, восстановлений {counts[2]}, без изменений {counts[3]}.\nПрименить в течение часа: /site_import_confirm {token}\nСуществующие записи не удаляются.'
        if command == '/site_import_confirm':
            token = arguments.strip()
            row = self.db.execute('SELECT * FROM site_imports WHERE token=?', (token,)).fetchone()
            if not row or row['expires'] < time.time():
                raise ValueError('Предпросмотр не найден или истёк. Повторите /site_import.')
            staged = json.loads(row['payload'])
            if staged['fingerprint'] != self.fingerprint():
                raise ValueError('Каталог изменился после предпросмотра. Повторите /site_import перед применением.')
            plan, counts = self.import_plan(staged['catalog'])
            for key, item, section in plan:
                self.save_item(key, item, False, section)
            self.db.execute('DELETE FROM site_imports WHERE token=?', (token,))
            return f'Импорт применён: новых {counts[0]}, изменений {counts[1]}, восстановлений {counts[2]}, без изменений {counts[3]}.'
        if command in ('/site_rename', '/site_note'):
            parts = arguments.split(maxsplit=1)
            if len(parts) != 2 or parts[0] not in entries:
                raise ValueError('Укажите ID и новый текст: ' + command + ' ID текст')
            key, value = parts
            item = {name: value for name, value in entries[key].items() if name != 'id'}
            if command == '/site_rename':
                item['name'] = value.strip()
            elif value.strip() == '-':
                item.pop('note', None)
            else:
                item['note'] = value.strip()
            validate_catalog([{'title': ADDED, 'items': [item]}])
            sites_messages([{'title': ADDED, 'items': [item | {'id': key}]}])
            old = self.db.execute('SELECT hidden FROM site_overrides WHERE id=?', (key,)).fetchone()
            self.save_item(key, item, bool(old and old['hidden']), sections[key])
            return 'Запись обновлена: ' + item['name'] + '\nID: ' + key
        if command == '/site_add':
            parts = arguments.split(maxsplit=1)
            if not parts:
                raise ValueError('Пример: /site_add https://example.com Название | примечание')
            url = parts[0]
            name, _, note = (parts[1] if len(parts) == 2 else '').partition('|')
            name = name.strip() or (urlsplit(url).hostname or 'Новая ссылка')
            item = {'name': name, 'url': url}
            if note.strip():
                item['note'] = note.strip()
            validate_catalog([{'title': ADDED, 'items': [item]}])
            key = bookmark_id(item)
            old = self.db.execute('SELECT hidden FROM site_overrides WHERE id=?', (key,)).fetchone()
            if key in entries:
                if identity(entries[key]) != identity(item):
                    raise ValueError('Конфликт идентификаторов ссылок')
                if old and old['hidden']:
                    self.db.execute('UPDATE site_overrides SET hidden=0 WHERE id=?', (key,))
                    return 'Ссылка восстановлена: ' + entries[key]['name']
                return 'Уже есть в каталоге: ' + entries[key]['name'] + '\nID: ' + key
            hidden = {row['id'] for row in self.db.execute('SELECT id FROM site_overrides WHERE hidden=1')}
            if sum(item['id'] not in hidden for item in entries.values()) >= 1000:
                raise ValueError('В каталоге уже 1000 записей; удалите ненужные перед добавлением.')
            # Ensure the item is renderable before changing persistent state.
            sites_messages([{'title': ADDED, 'items': [item | {'id': key}]}])
            self.db.execute('INSERT INTO site_overrides(id,item,hidden) VALUES (?,?,0)',
                (key, json.dumps(item, ensure_ascii=False)))
            return 'Добавлено в каталог: ' + name + '\nID: ' + key + '\nПосмотреть: /sites ' + key
        if command not in ('/site_del', '/site_restore'):
            raise ValueError('Неизвестная команда каталога')
        key = arguments.strip()
        if not re.fullmatch(r'[sn][0-9a-f]{12}', key):
            raise ValueError('Укажите ID из /sites, например: ' + command + ' s0123456789ab')
        item = entries.get(key)
        if item is None:
            raise ValueError('Запись не найдена. Получите актуальный ID через /sites.')
        hidden = command == '/site_del'
        saved = {name: value for name, value in item.items() if name != 'id'}
        # Keep a copy of a removed built-in bookmark so it remains restorable
        # even when a later bundled/custom JSON no longer contains that URL.
        self.save_item(key, saved, hidden, sections[key])
        if hidden:
            return 'Удалено из каталога: ' + item['name'] + '\nВосстановить: /site_restore ' + key
        return 'Восстановлено в каталоге: ' + item['name']

    def save_item(self, key, item, hidden, section):
        saved = {name: value for name, value in item.items() if name != 'id'}
        saved['_section'] = section
        self.db.execute('INSERT INTO site_overrides(id,item,hidden) VALUES (?,?,?) '
            'ON CONFLICT(id) DO UPDATE SET item=excluded.item,hidden=excluded.hidden',
            (key, json.dumps(saved, ensure_ascii=False), hidden))

    def fingerprint(self):
        view = self.view(include_hidden=True)
        hidden = sorted(row['id'] for row in self.db.execute('SELECT id FROM site_overrides WHERE hidden=1'))
        return hashlib.sha256(json.dumps([view, hidden], ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def import_plan(self, incoming):
        if incoming != []:
            validate_catalog(incoming, max_sections=1000)
        current = {item['id']: item for section in self.view(include_hidden=True) for item in section['items']}
        hidden = {row['id'] for row in self.db.execute('SELECT id FROM site_overrides WHERE hidden=1')}
        plan, wanted, counts = [], {}, [0, 0, 0, 0]
        for section in incoming:
            for raw in section['items']:
                item = {key: raw[key] for key in ('name', 'url', 'note') if key in raw}
                key = bookmark_id(item)
                if not item.get('url') and isinstance(raw.get('id'), str) and re.fullmatch(r'n[0-9a-f]{12}', raw['id']):
                    key = raw['id']
                if key in wanted:
                    if wanted[key] != item:
                        raise ValueError('В импорте несколько разных версий одной записи.')
                    continue
                wanted[key] = item
                old = current.get(key)
                if old and item.get('url') and identity(old) != identity(item):
                    raise ValueError('Конфликт идентификаторов в импорте.')
                sites_messages([{'title': section['title'], 'items': [item | {'id': key}]}])
                if old is None:
                    counts[0] += 1
                elif key in hidden:
                    counts[2] += 1
                elif {k: v for k, v in old.items() if k != 'id'} != item:
                    counts[1] += 1
                else:
                    counts[3] += 1
                plan.append((key, item, section['title']))
        visible = set(current) - hidden
        if len(visible | set(wanted)) > 1000:
            raise ValueError('После импорта будет больше 1000 видимых записей.')
        return plan, counts
