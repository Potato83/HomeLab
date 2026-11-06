from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import URLError

from botwork.bookmarks import BookmarkCatalog,bookmark_id
from botwork.documents import send_json,read_json
from botwork.sites import handle_sites
from botwork.storage import Store
from botwork.telegram import Telegram
from botwork.http import FetchError

BASE=[{'title':'База','items':[{'name':'Работа','url':'https://example.com','note':'Примечание'}, {'name':'Заметка','note':'Локальный путь'}]}]

class CatalogEditTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'sites.json'
        self.path.write_text(json.dumps(BASE));self.env=patch.dict('os.environ',{'BOTWORK_SITES':str(self.path)});self.env.start()
        self.store=Store(str(Path(self.temp.name)/'db'));self.repo=BookmarkCatalog(self.store);self.seq=0
    def tearDown(self):self.store.close();self.env.stop();self.temp.cleanup()
    def execute(self,command,args):
        self.seq+=1;return self.repo.execute(command,args,self.seq)
    def key(self,index=0):return bookmark_id(BASE[0]['items'][index])
    def item(self,key):return next(i for s in self.repo.view(True) for i in s['items'] if i['id']==key)
    def stage(self,data):
        reply=self.execute('/site_import',json.dumps(data))
        token=re.search(r'/site_import_confirm ([0-9a-f]+)',reply)
        self.assertIsNotNone(token,reply);return token[1]

    def test_rename_and_note_preserve_ids_including_local_notes(self):
        before=self.path.read_bytes()
        for index in (0,1):
            key=self.key(index)
            self.execute('/site_rename',key+' Новое название')
            self.execute('/site_note',key+' Новый текст')
            self.assertEqual(self.item(key)['name'],'Новое название')
            self.assertEqual(self.item(key)['note'],'Новый текст')
        self.execute('/site_note',self.key()+' -');self.assertNotIn('note',self.item(self.key()))
        self.assertEqual(before,self.path.read_bytes())

    def test_edit_hidden_does_not_restore_it(self):
        key=self.key();self.execute('/site_del',key)
        self.execute('/site_rename',key+' Новое название')
        self.assertEqual(self.repo.hidden()[0]['items'][0]['name'],'Новое название')
        self.execute('/site_restore',key)
        self.assertEqual(self.item(key)['name'],'Новое название')

    def test_invalid_edit_does_not_modify(self):
        before=self.repo.view()
        self.execute('/site_rename',self.key()+' '+'x'*201)
        self.execute('/site_note',self.key()+' '+'x'*501)
        self.assertEqual(before,self.repo.view())

    def test_import_preview_no_changes_until_confirm_and_replay(self):
        incoming=[{'title':'Импорт','items':[{'name':'Новая работа','url':'https://new.example/jobs'}]}]
        token=self.stage(incoming);self.assertEqual(self.repo.view()[0]['items'][0]['name'],'Работа')
        self.assertEqual(sum(len(s['items']) for s in self.repo.view()),2)
        reply=self.execute('/site_import_confirm',token)
        self.assertIn('новых 1',reply)
        self.assertEqual(sum(len(s['items']) for s in self.repo.view()),3)
        self.assertEqual(self.repo.view()[-1]['title'],'Импорт')
        self.assertEqual(self.repo.execute('/site_import_confirm',token,self.seq),reply)

    def test_export_import_roundtrip_updates_and_restores_without_deleting(self):
        key=self.key();incoming=deepcopy(self.repo.view());incoming[0]['items'][0]['name']='Новое'
        self.execute('/site_del',key);token=self.stage(incoming)
        self.assertIn('восстановлений 1',self.execute('/site_import_confirm',token))
        self.assertEqual(self.item(key)['name'],'Новое')
        self.assertEqual(sum(len(s['items']) for s in self.repo.view()),2)

    def test_stale_preview_and_expired_preview_rejected(self):
        token=self.stage(BASE);self.execute('/site_rename',self.key()+' Другое')
        self.assertIn('Каталог изменился',self.execute('/site_import_confirm',token))
        token=self.stage(BASE)
        with self.store.db:self.store.db.execute('UPDATE site_imports SET expires=0 WHERE token=?',(token,))
        self.assertIn('истёк',self.execute('/site_import_confirm',token))

    def test_bad_and_conflicting_import_has_no_side_effects(self):
        before=self.repo.view()
        for data in [{'not':'a catalog'}, [{'title':'X','items':[{'name':'X','url':'http://example.com'}]}],
                     [{'title':'X','items':[{'name':'A','url':'https://x.example'},{'name':'B','url':'https://x.example'}]}]]:
            reply=self.execute('/site_import',json.dumps(data));self.assertNotIn('/site_import_confirm ',reply)
            self.assertEqual(before,self.repo.view())

    def test_owner_document_caption_only_and_export_calls_document_api(self):
        telegram=Telegram('fake','123');telegram.send=Mock();telegram.read_json=Mock(return_value=BASE);telegram.send_json=Mock()
        update={'update_id':1,'message':{'from': {'id': 123}, 'chat':{'id':456,'type':'private'},'caption':'/site_import','document':{'file_id':'x','file_size':30}}}
        self.assertFalse(handle_sites(telegram,self.store,update));telegram.read_json.assert_not_called()
        update['message']['chat']['id']=123
        self.assertTrue(handle_sites(telegram,self.store,update));telegram.read_json.assert_called_once()
        update['message']['text']='/site_export'
        handle_sites(telegram,self.store,update);telegram.send_json.assert_called_once()
        self.assertEqual(telegram.send_json.call_args.args[0],'botwork-sites.json')

    def test_export_only_added_links(self):
        self.execute('/site_add','https://new.example Новая')
        self.execute('/site_rename',self.key()+' Переименованная базовая')
        telegram=Telegram('fake','123');telegram.send_json=Mock()
        handle_sites(telegram,self.store,{'message':{'from': {'id': 123}, 'chat':{'id':123,'type':'private'},'text':'/site_export added'}})
        items=[i for s in telegram.send_json.call_args.args[1] for i in s['items']]
        self.assertEqual(len(items),1);self.assertEqual(items[0]['url'],'https://new.example')

class DocumentTests(unittest.TestCase):
    def response(self,raw):
        response=Mock();response.read.return_value=raw
        context=Mock();context.__enter__=Mock(return_value=response);context.__exit__=Mock(return_value=False)
        return context
    def test_get_file_content_bounded_and_path_trusted(self):
        with patch('botwork.documents.telegram_call',return_value={'file_path':'documents/file_1.json'}),patch('botwork.documents.build_opener') as build:
            build.return_value.open.return_value=self.response(b'[]')
            self.assertEqual(read_json('SECRET',{'file_id':'1','file_size':2}),[])
        for path in ['../bad','https://example.com/file','/absolute']:
            with patch('botwork.documents.telegram_call',return_value={'file_path':path}),self.assertRaises(FetchError):
                read_json('SECRET',{'file_id':'1','file_size':2})
    def test_document_errors_do_not_expose_token(self):
        with patch('botwork.documents.build_opener') as build:
            build.return_value.open.side_effect=URLError('https://api.telegram.org/SECRET')
            with self.assertRaises(FetchError) as error:send_json('SECRET','123','sites.json',[])
            self.assertNotIn('SECRET',str(error.exception))
    def test_upload_sends_json_to_telegram_without_redirects(self):
        with patch('botwork.documents.build_opener') as build:
            build.return_value.open.return_value=self.response(b'{"ok":true,"result":{"message_id":7}}')
            self.assertEqual(send_json('SECRET','123','sites.json',[{'name':'Работа'}]),{'message_id':7})
            request=build.return_value.open.call_args.args[0]
            self.assertIn('name="document"',request.data.decode())
            self.assertIn('Работа',request.data.decode())
    def test_invalid_size_and_json(self):
        for size in [None,-1,1000001]:
            with self.assertRaises(ValueError):read_json('SECRET',{'file_id':'1','file_size':size})
        with patch('botwork.documents.telegram_call',return_value={'file_path':'documents/file_1.json'}),patch('botwork.documents.build_opener') as build:
            build.return_value.open.return_value=self.response(b'not JSON')
            with self.assertRaises(ValueError):read_json('SECRET',{'file_id':'1','file_size':8})

if __name__=='__main__':unittest.main()
