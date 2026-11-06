from contextlib import redirect_stdout
from html.parser import HTMLParser
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from botwork.__main__ import HELP, main
from botwork.sites import load_sites, sites_messages, send_sites, length, LIMIT
from botwork.telegram import Telegram


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.depth = 0

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.links.append(dict(attrs)['href'])
            self.depth += 1

    def handle_endtag(self, tag):
        if tag == 'a':
            self.depth -= 1


class SitesTests(unittest.TestCase):
    def test_default_includes_originals_and_additions_once(self):
        catalog = load_sites()
        urls = [i['url'] for section in catalog for i in section['items'] if i.get('url')]
        self.assertEqual(len(urls), 56)
        self.assertEqual(len(set(urls)), 56)
        for url in ['https://job.beeline.ru/', 'https://alfabank.sale/alfafuture/event/hack/#form',
                    'https://career.infotecs.ru/', 'https://career.infotecs.ru/intern/',
                    'https://ozon.tech/routestart/?__rr=1&abt_att=1', 'https://github.com/fikstt2/hh-ai-agent',
                    'https://hh.ru/', 'https://career.habr.com/vacancies']:
            self.assertIn(url, urls)
        text = '\n'.join(sites_messages(catalog))
        self.assertIn('DevOps Projects', text)
        self.assertIn('DevSecOps', text)
        self.assertIn('Дополнительно', text)

    def test_large_catalog_is_split_without_lost_or_broken_links(self):
        catalog = [{'title': 'Поиск', 'items': [{'name': 'Ресурс ' + str(i) + ' 🔐' * 40,
                    'url': 'https://example.com/' + str(i), 'note': 'Описание' * 10} for i in range(100)]}]
        messages = sites_messages(catalog)
        self.assertGreater(len(messages), 1)
        parser = Links()
        for message in messages:
            self.assertLessEqual(length(message), LIMIT)
            parser.feed(message)
            self.assertEqual(parser.depth, 0)
        self.assertEqual(len(parser.links), 100)
        self.assertEqual(len(set(parser.links)), 100)

    def test_catalog_html_is_escaped(self):
        catalog = [{'title': '<Title>', 'items': [{'name': '<script>', 'url': 'https://example.com/?a=1&b=2', 'note': '<b>not markup</b>'}]}]
        text = sites_messages(catalog)[0]
        self.assertNotIn('<script>', text)
        self.assertIn('&lt;script&gt;', text)
        parser = Links()
        parser.feed(text)
        self.assertEqual(parser.links, ['https://example.com/?a=1&b=2'])

    def test_invalid_catalog_and_urls_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sites.json'
            for url in ['javascript:alert(1)', 'http://example.com', 'https://user:password@example.com', 'https://example.com:bad', 'https://example.com|Title', 'https://-example.com', 'https://999.999.999.999']:
                path.write_text(json.dumps([{'title': 'Каталог', 'items': [{'name': 'X', 'url': url}]}]))
                with self.assertRaises(ValueError):
                    load_sites(path)
            for text in ['{}', '[]', 'not JSON', '[{"title":"Empty","items":[]}]']:
                path.write_text(text)
                with self.assertRaises(ValueError):
                    load_sites(path)

    def test_custom_catalog_via_env_and_safe_error_reply(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sites.json'
            path.write_text('[{"title":"Мой раздел","items":[{"name":"Мой сайт","url":"https://example.com"}]}]')
            telegram = Mock()
            with patch.dict('os.environ', {'BOTWORK_SITES': str(path)}):
                send_sites(telegram)
                self.assertIn('Мой сайт', telegram.send.call_args.args[0])
                path.write_text('broken')
                send_sites(telegram)
                self.assertIn('Не удалось прочитать', telegram.send.call_args.args[0])

    def test_cli_sites_does_not_need_config_database_or_telegram(self):
        output = StringIO()
        with patch('sys.argv', ['botwork', 'sites', '--config', '/missing/config.toml', '--json']), \
             patch('botwork.__main__.load_config') as config, patch('botwork.__main__.Store') as store, \
             patch('botwork.__main__.Telegram') as telegram, redirect_stdout(output):
            self.assertEqual(main(), 0)
            self.assertEqual(len(json.loads(output.getvalue())), 10)
            config.assert_not_called()
            store.assert_not_called()
            telegram.assert_not_called()

    def test_only_owner_private_chat_can_request_sites(self):
        telegram = Telegram('fake', '123')
        update = {'message': {'from': {'id': 123}, 'text': '/sites', 'chat': {'id': 123, 'type': 'private'}}}
        self.assertEqual(telegram.command(update), '/sites')
        for chat in [{'id': 456, 'type': 'private'}, {'id': 123, 'type': 'group'}]:
            update['message']['chat'] = chat
            self.assertIsNone(telegram.command(update))
        self.assertIn('/sites', HELP)


if __name__ == '__main__':
    unittest.main()
