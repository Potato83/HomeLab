from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from workbot.__main__ import main
from workbot.bookmarks import BookmarkCatalog, bookmark_id
from workbot.http import FetchError
from workbot.maintenance import backup_database
from workbot.sites import handle_sites, filter_sites, sites_messages
from workbot.storage import Store
from workbot.telegram import Telegram

BASE = [{'title': 'Карьера', 'items': [
    {'name': 'PT Start', 'url': 'https://pt-start.ru/', 'note': 'Стажировки ИБ'},
    {'name': 'Яндекс', 'url': 'https://yandex.ru/yaintern/internship'},
    {'name': 'DevSecOps', 'note': 'Мои материалы'}]}]


class BookmarkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / 'db.sqlite3')
        self.json_path = Path(self.temp.name) / 'sites.json'
        self.json_path.write_text(json.dumps(BASE, ensure_ascii=False))
        self.env = patch.dict('os.environ', {'WORKBOT_SITES': str(self.json_path)})
        self.env.start()
        self.store = Store(self.path)
        self.catalog = BookmarkCatalog(self.store)
        self.update = 0

    def tearDown(self):
        self.store.close()
        self.env.stop()
        self.temp.cleanup()

    def execute(self, command, arguments):
        self.update += 1
        return self.catalog.execute(command, arguments, self.update)

    def items(self):
        return [item for section in self.catalog.view() for item in section['items']]

    def test_add_search_restart_and_readonly_base(self):
        before = self.json_path.read_bytes()
        response = self.execute('/site_add', 'https://example.com/jobs Вакансии ИБ | Удалённая работа')
        self.assertIn('Добавлено', response)
        matches = filter_sites(self.catalog.view(), 'вакансии удаленная')
        self.assertEqual(matches[0]['items'][0]['name'], 'Вакансии ИБ')
        self.assertEqual(self.json_path.read_bytes(), before)
        self.store.close()
        self.store = Store(self.path)
        self.catalog = BookmarkCatalog(self.store)
        self.assertEqual(len(self.items()), 4)
        self.assertIn('Удалённая работа', self.items()[-1]['note'])

    def test_duplicate_url_normalized_and_title_not_overwritten(self):
        self.execute('/site_add', 'https://EXAMPLE.com:443 Работа')
        response = self.execute('/site_add', 'https://example.com/ Другое имя')
        self.assertIn('Уже есть', response)
        self.assertEqual(len(self.items()), 4)
        self.assertEqual(self.items()[-1]['name'], 'Работа')

    def test_existing_builtin_duplicate_does_not_add(self):
        response = self.execute('/site_add', 'https://pt-start.ru Переименованный')
        self.assertIn('Уже есть', response)
        self.assertEqual(len(self.items()), 3)

    def test_hide_restore_builtin_and_note_without_changing_file(self):
        for item in [BASE[0]['items'][0], BASE[0]['items'][2]]:
            key = bookmark_id(item)
            before = self.json_path.read_bytes()
            response = self.execute('/site_del', key)
            self.assertIn('/site_restore ' + key, response)
            self.assertNotIn(key, [item['id'] for item in self.items()])
            self.execute('/site_restore', key)
            self.assertIn(key, [item['id'] for item in self.items()])
            self.assertEqual(before, self.json_path.read_bytes())

    def test_deleted_builtin_restorable_after_base_file_update(self):
        key = bookmark_id(BASE[0]['items'][0])
        self.execute('/site_del', key)
        self.json_path.write_text(json.dumps([{'title': 'Обновлённый', 'items': [BASE[0]['items'][1]]}]))
        self.execute('/site_restore', key)
        self.assertIn(key, [item['id'] for item in self.items()])
        self.assertEqual(next(i['name'] for i in self.items() if i['id'] == key), 'PT Start')

    def test_hidden_list_shows_ids_and_restore_hint(self):
        key = bookmark_id(BASE[0]['items'][0])
        self.execute('/site_del', key)
        telegram = Telegram('fake', '123')
        telegram.send = Mock()
        update = {'message': {'from': {'id': 123}, 'chat': {'id': 123, 'type': 'private'}, 'text': '/site_hidden'}}
        handle_sites(telegram, self.store, update)
        text = telegram.send.call_args.args[0]
        self.assertIn(key, text)
        self.assertIn('/site_restore', text)
        self.assertNotIn('Яндекс', text)
        self.execute('/site_restore', key)
        handle_sites(telegram, self.store, update)
        self.assertIn('Удалённых ссылок нет', telegram.send.call_args.args[0])

    def test_added_link_deleted_and_add_same_url_restores(self):
        self.execute('/site_add', 'https://example.com Работа')
        key = self.items()[-1]['id']
        self.execute('/site_del', key)
        self.assertEqual(len(self.items()), 3)
        response = self.execute('/site_add', 'https://example.com/ Новый текст')
        self.assertIn('восстановлена', response)
        self.assertEqual(len(self.items()), 4)
        self.assertEqual(self.items()[-1]['name'], 'Работа')

    def test_ids_stable_after_reordering_and_renaming(self):
        before = {i.get('url'): i['id'] for i in self.items() if i.get('url')}
        base = json.loads(self.json_path.read_text())
        base[0]['items'].reverse()
        base[0]['items'][-1]['name'] = 'Новое название PT'
        self.json_path.write_text(json.dumps(base))
        after = {i.get('url'): i['id'] for i in self.items() if i.get('url')}
        self.assertEqual(before, after)

    def test_update_replay_returns_original_reply_without_reapplying(self):
        response = self.catalog.execute('/site_add', 'https://example.com Работа', 100)
        key = self.items()[-1]['id']
        self.catalog.execute('/site_del', key, 101)
        self.assertEqual(self.catalog.execute('/site_add', 'https://example.com Работа', 100), response)
        self.assertNotIn(key, [i['id'] for i in self.items()])

    def test_failed_reply_retry_preserves_reply_and_single_add(self):
        telegram = Telegram('fake', '123')
        telegram.send = Mock(side_effect=[FetchError('offline'), None])
        update = {'update_id': 42, 'message': {'from': {'id': 123}, 'chat': {'id': 123, 'type': 'private'},
            'text': '/site_add https://example.com Работа'}}
        with self.assertRaises(FetchError):
            handle_sites(telegram, self.store, update)
        self.assertEqual(len(self.items()), 4)
        handle_sites(telegram, self.store, update)
        self.assertEqual(telegram.send.call_args_list[0].args, telegram.send.call_args_list[1].args)
        self.assertEqual(len(self.items()), 4)

    def test_invalid_commands_do_not_change_catalog(self):
        for command, value in [('/site_add', ''), ('/site_add', 'http://example.com X'),
            ('/site_add', 'https://u:p@example.com X'), ('/site_add', 'javascript:alert(1) X'),
            ('/site_add', 'https://example.com ' + 'x' * 201), ('/site_del', '1'),
            ('/site_restore', 's0123456789ab')]:
            with self.subTest(command=command, value=value):
                before = self.items()
                self.execute(command, value)
                self.assertEqual(before, self.items())

    def test_bad_base_json_preserves_saved_edits_and_recovers(self):
        self.execute('/site_add', 'https://example.com Работа')
        self.json_path.write_text('broken')
        response = self.execute('/site_add', 'https://another.example Работа')
        self.assertIn('Не удалось прочитать', response)
        self.json_path.write_text(json.dumps(BASE))
        self.assertEqual(len(self.items()), 4)

    def test_mutations_only_in_owner_private_chat_and_have_update_id(self):
        telegram = Telegram('fake', '123')
        telegram.send = Mock()
        for chat in [{'id': 456, 'type': 'private'}, {'id': 123, 'type': 'group'}]:
            update = {'update_id': 1, 'message': {'from': {'id': 123}, 'chat': chat, 'text': '/site_add https://example.com X'}}
            self.assertFalse(handle_sites(telegram, self.store, update))
        self.assertEqual(len(self.items()), 3)
        telegram.send.assert_not_called()
        with self.assertRaises(ValueError):
            self.catalog.execute('/site_add', 'https://example.com X', None)

    def test_arguments_and_search_html_escape_no_results(self):
        telegram = Telegram('fake', '123')
        telegram.send = Mock()
        update = {'message': {'from': {'id': 123}, 'chat': {'id': 123, 'type': 'private'}, 'text': '/sites@MyBot PT старт'}}
        self.assertEqual(telegram.command(update), '/sites')
        self.assertEqual(telegram.arguments(update), 'PT старт')
        update['message']['text'] = '/sites PT'
        self.assertTrue(handle_sites(telegram, self.store, update))
        text = telegram.send.call_args.args[0]
        self.assertIn('PT Start', text)
        self.assertNotIn('Яндекс', text)
        self.assertIn('<code>s', text)
        update['message']['text'] = '/sites <нет>'
        handle_sites(telegram, self.store, update)
        self.assertIn('&lt;нет&gt;', telegram.send.call_args.args[0])
        self.assertIn('Ничего не найдено', telegram.send.call_args.args[0])

    def test_malicious_name_is_escaped_in_reply_and_listing(self):
        telegram = Telegram('fake', '123')
        telegram.send = Mock()
        update = {'update_id': 1, 'message': {'from': {'id': 123}, 'chat': {'id': 123, 'type': 'private'},
            'text': '/site_add https://example.com <script> | <b>text</b>'}}
        handle_sites(telegram, self.store, update)
        self.assertIn('&lt;script&gt;', telegram.send.call_args.args[0])
        self.assertNotIn('<script>', telegram.send.call_args.args[0])
        self.assertIn('&lt;b&gt;', '\n'.join(sites_messages(self.catalog.view())))

    def test_concurrent_adds_have_one_bookmark(self):
        def add(update):
            store = Store(self.path)
            try:
                return BookmarkCatalog(store).execute('/site_add', 'https://example.com Работа', update)
            finally:
                store.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(add, [10, 11]))
        self.assertEqual(len(self.items()), 4)
        self.assertEqual(sum('Добавлено' in r for r in responses), 1)

    def test_backup_includes_edits_and_replay_receipts(self):
        self.execute('/site_add', 'https://example.com Работа')
        self.execute('/site_del', bookmark_id(BASE[0]['items'][0]))
        backup = str(Path(self.temp.name) / 'backup.sqlite3')
        backup_database(self.path, backup)
        restored = Store(backup)
        try:
            self.assertEqual(BookmarkCatalog(restored).view(), self.catalog.view())
            response = BookmarkCatalog(restored).execute('/site_add', 'https://example.com Работа', 1)
            self.assertIn('Добавлено', response)
        finally:
            restored.close()

    def test_cli_saved_search_and_export_without_telegram(self):
        self.execute('/site_add', 'https://example.com Работа')
        output = StringIO()
        with patch.dict('os.environ', {'WORKBOT_DB': self.path}), \
             patch('sys.argv', ['workbot', 'sites', '--saved', '--query', 'example.com', '--json']), \
             patch('workbot.__main__.logging.basicConfig'), \
             patch('workbot.__main__.Telegram') as telegram, redirect_stdout(output):
            self.assertEqual(main(), 0)
            result = json.loads(output.getvalue())
            self.assertEqual(result[0]['items'][0]['url'], 'https://example.com')
            telegram.assert_not_called()

    def test_large_search_query_gets_friendly_response(self):
        telegram = Telegram('fake', '123')
        telegram.send = Mock()
        update = {'message': {'from': {'id': 123}, 'chat': {'id': 123, 'type': 'private'}, 'text': '/sites ' + 'x' * 201}}
        handle_sites(telegram, self.store, update)
        self.assertIn('200', telegram.send.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
