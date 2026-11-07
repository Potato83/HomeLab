"""Exercise owner intake through real aiogram dispatch, without network or real secrets."""
import asyncio
from datetime import UTC, datetime
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.methods import DeleteMessage, SendMessage
from aiogram.types import CallbackQuery, Chat, Document, Message, Update, User

from agentbot.handlers import MAX_FILE
from agentbot.main import dispatcher
from agentbot.store import Store
from agentbot.ui import NEW_TASK


class IntakeHarness:
    def __init__(self, path):
        self.store = Store(path)
        self.index = 0
        self.session_index = 0
        self.events = []
        self.uploads = []
        self.download_bytes = b'raise Exception("must remain data")\n'
        self.backend = SimpleNamespace(
            create=AsyncMock(side_effect=self.create),
            intake=AsyncMock(return_value={'prompt': 'Read the private task workspace and solve the assigned task.'}),
            save_access=AsyncMock(side_effect=self.save_access),
            upload=AsyncMock(side_effect=self.upload),
        )
        self.bot = Bot('123456:' + 'A' * 35)
        self.bot.session.make_request = AsyncMock(side_effect=self.telegram)
        self.bot.download = AsyncMock(side_effect=self.download)
        self.dp = dispatcher(self.store, self.backend, 123)

    async def create(self):
        self.session_index += 1
        return f'ses_backend_private_{self.session_index}'

    async def save_access(self, sid, access):
        self.events.append(('saved', sid))

    async def upload(self, sid, path, name, *, upload_id):
        data = path.read_bytes()
        self.uploads.append(data)
        return {'complete': True, 'name': name, 'path': '/private/task/files/original',
                'size': len(data), 'upload_id': upload_id}

    async def download(self, file_id, *, destination):
        destination.write(self.download_bytes)

    async def telegram(self, bot, method, **kwargs):
        if isinstance(method, DeleteMessage):
            self.events.append(('deleted', method.message_id))
        return True

    def update_number(self):
        self.index += 1
        return self.index

    async def send(self, text='', *, user=123, chat_type='private', document=None, caption=None):
        index = self.update_number()
        message = Message(
            message_id=index, date=datetime.now(UTC),
            chat=Chat(id=user if chat_type == 'private' else -999, type=chat_type),
            from_user=User(id=user, is_bot=False, first_name='Fixture owner'),
            text=text or None, document=document, caption=caption,
        )
        await self.dp.feed_update(self.bot, Update(update_id=index, message=message))
        return index

    async def click(self, data, *, user=123, chat_type='private'):
        index = self.update_number()
        message = Message(
            message_id=index, date=datetime.now(UTC),
            chat=Chat(id=123 if chat_type == 'private' else -999, type=chat_type),
            from_user=User(id=999, is_bot=True, first_name='Fixture bot'),
            text='Task card',
        )
        query = CallbackQuery(
            id=f'callback_{index}', chat_instance='fixture_chat', data=data,
            from_user=User(id=user, is_bot=False, first_name='Fixture user'), message=message,
        )
        await self.dp.feed_update(self.bot, Update(update_id=index, callback_query=query))
        return index

    @property
    def sent(self):
        return [call.args[1] for call in self.bot.session.make_request.await_args_list
                if isinstance(call.args[1], SendMessage)]

    @property
    def texts(self):
        return '\n'.join(method.text for method in self.sent)

    @property
    def database_text(self):
        rows = []
        for table in ['sessions', 'meta', 'runs']:
            rows += [tuple(row) for row in self.store.db.execute('SELECT * FROM ' + table)]
        return json.dumps(rows)

    async def close(self):
        await self.bot.session.close()
        self.store.close()


@pytest.mark.parametrize('mode', ['password','token'])
def test_replayed_secret_after_restart_is_not_interpreted_as_task_scope(tmp_path,mode):
    async def run():
        path=tmp_path/'state.db'
        h=IntakeHarness(path)
        secret='fixture-secret-replayed-after-process-restart'
        try:
            await h.send('/board')
            await h.send('https://ctf.example/challenges')
            await h.click(f'access_{mode}:1')
            if mode=='password':await h.send('FixtureLogin')
            uid=await h.send(secret)
            assert h.store.wizard()['stage']=='scope'
            h.store.close()
            h.store=Store(path)
            h.dp=dispatcher(h.store,h.backend,123)
            message=Message(message_id=uid,date=datetime.now(UTC),chat=Chat(id=123,type='private'),
                            from_user=User(id=123,is_bot=False,first_name='Fixture owner'),text=secret)
            await h.dp.feed_update(h.bot,Update(update_id=uid,message=message))
            assert h.store.wizard()['stage']=='scope'
            assert h.backend.save_access.await_count==1
            assert 'scope' not in h.store.wizard()['payload']
            assert secret not in h.database_text and secret not in h.texts
        finally:await h.close()
    asyncio.run(run())


def test_password_form_does_not_claim_access_saved_before_password(tmp_path):
    async def run():
        h=IntakeHarness(tmp_path/'state.db')
        try:
            await h.send('/board');await h.send('https://ctf.example')
            await h.click('access_password:1')
            await h.click('status:1')
            assert 'логин и пароль сохранены' not in h.sent[-1].text
            h.backend.save_access.assert_not_awaited()
        finally:await h.close()
    asyncio.run(run())


def test_start_exposes_native_menu_and_commands(tmp_path):
    async def run():
        h = IntakeHarness(tmp_path / 'state.db')
        try:
            await h.send('/start')
            message = h.sent[-1]
            assert message.reply_markup.is_persistent is True
            assert message.reply_markup.resize_keyboard is True
            buttons = [b.text for row in message.reply_markup.keyboard for b in row]
            assert NEW_TASK in buttons and '🌐 Борда' in buttons and '⏹ Остановить' in buttons
            assert all(command in message.text for command in ['/new', '/status', '/stop', '/help'])
            h.backend.create.assert_not_awaited()
        finally:
            await h.close()
    asyncio.run(run())


@pytest.mark.parametrize(('user', 'chat_type'), [(456, 'private'), (123, 'group')])
def test_unauthorized_callbacks_are_denied_before_state_or_transport(tmp_path, user, chat_type):
    async def run():
        h = IntakeHarness(tmp_path / 'state.db')
        try:
            await h.send('/new')
            await h.send('Task title\nPrivate fixture condition')
            before = h.database_text
            h.bot.session.make_request.reset_mock()
            await h.click('start:1', user=user, chat_type=chat_type)
            assert h.database_text == before
            h.backend.intake.assert_not_awaited()
            h.backend.save_access.assert_not_awaited()
            h.bot.session.make_request.assert_not_awaited()
        finally:
            await h.close()
    asyncio.run(run())


def test_task_title_condition_preview_and_explicit_start(tmp_path):
    async def run():
        h = IntakeHarness(tmp_path / 'state.db')
        try:
            await h.send('/new')
            assert h.store.wizard()['stage'] == 'task_text'
            await h.send('Weird RSA\nSolve only the supplied original files.')
            sid = h.store.current()
            row = h.store.session(sid)
            assert row['number'] == 1 and row['title'] == 'Weird RSA'
            assert h.store.wizard()['stage'] == 'review'
            assert '#1 · Weird RSA' in h.texts
            assert sid not in h.texts
            h.backend.intake.assert_not_awaited()
            uid = await h.click('start:1')
            h.backend.intake.assert_awaited_once()
            args = h.backend.intake.await_args.args
            assert args[0] == sid
            assert args[1]['title'] == 'Weird RSA'
            assert args[1]['description'] == 'Weird RSA\nSolve only the supplied original files.'
            assert h.store.get(uid)['state'] == 'prepared'
            assert h.store.get(uid)['prompt'] == h.backend.intake.return_value['prompt']
            assert h.store.session(sid)['stage'] == 'active' and h.store.wizard() is None
        finally:
            await h.close()
    asyncio.run(run())


def test_board_password_is_remote_only_and_deleted_after_success(tmp_path):
    async def run():
        h = IntakeHarness(tmp_path / 'state.db')
        secret = 'fixture-password-never-persist'
        try:
            await h.send('/board')
            await h.send('https://ctf.example/training')
            await h.click('access_password:1')
            await h.send('HarnessFixtureUser')
            assert h.store.wizard()['stage'] == 'password'
            password_message_id = await h.send(secret)
            h.backend.save_access.assert_awaited_once_with(h.store.current(), {
                'mode': 'password', 'login': 'HarnessFixtureUser', 'password': secret})
            assert h.events.index(('saved', h.store.current())) < h.events.index(('deleted', password_message_id))
            assert secret not in h.database_text and secret not in h.texts
            await h.click('scope_all:1')
            await h.click('submit_no:1')
            assert h.store.wizard()['stage'] == 'review'
            uid = await h.click('start:1')
            payload = h.backend.intake.await_args.args[1]
            assert payload['board_url'] == 'https://ctf.example/training'
            assert payload['allow_submit'] is False and payload['scope']
            assert secret not in json.dumps(payload)
            assert 'HarnessFixtureUser' not in json.dumps(payload)
            assert secret not in h.store.get(uid)['prompt']
            assert secret not in h.database_text and secret not in h.texts
        finally:
            await h.close()
    asyncio.run(run())


@pytest.mark.parametrize(('mode', 'message_text'), [('password', 'fixture-private-password'), ('token', 'fixture-private-token')])
def test_failed_access_save_does_not_delete_or_persist_secret(tmp_path, mode, message_text):
    async def run():
        h = IntakeHarness(tmp_path / 'state.db')
        try:
            await h.send('/board')
            await h.send('https://ctf.example')
            await h.click(f'access_{mode}:1')
            if mode == 'password':
                await h.send('FixtureUser')
            h.events.clear()
            h.backend.save_access.side_effect = RuntimeError('Upstream error containing ' + message_text)
            uid = await h.send(message_text)
            assert ('deleted', uid) not in h.events
            assert message_text not in h.database_text and message_text not in h.texts
            assert h.store.wizard()['stage'] == mode
            h.backend.intake.assert_not_awaited()
        finally:
            await h.close()
    asyncio.run(run())


def test_persistent_wizard_resumes_after_store_restart(tmp_path):
    async def run():
        path = tmp_path / 'state.db'
        h = IntakeHarness(path)
        await h.send('/new')
        await h.send('Restart task\nPreserve the unfinished description.')
        await h.close()
        h = IntakeHarness(path)
        try:
            assert h.store.wizard()['stage'] == 'review'
            assert h.store.wizard()['payload']['title'] == 'Restart task'
            uid = await h.click('start:1')
            assert h.store.get(uid)['state'] == 'prepared'
            assert h.backend.intake.await_args.args[1]['description'].endswith('unfinished description.')
        finally:
            await h.close()
    asyncio.run(run())


@pytest.mark.parametrize('open_method', ['picker','status'])
@pytest.mark.parametrize(('stage','question','buttons'), [
    ('task_text','название и условие',[]),
    ('board_url','адрес CTFd',[]),
    ('access','Как входить',['access_password:1','access_token:1']),
    ('login','логин',[]),
    ('password','пароль',[]),
    ('token','API-токен',[]),
    ('scope','Какие задачи',['scope_all:1']),
    ('submit','отправка',['submit_yes:1','submit_no:1']),
])
def test_reopening_incomplete_form_restores_question_and_controls_after_restart(tmp_path,open_method,stage,question,buttons):
    async def run():
        path=tmp_path/'state.db'
        h=IntakeHarness(path)
        login='fixture-private-login'
        secret='fixture-private-access-secret'
        try:
            await h.send('/new' if stage=='task_text' else '/board')
            if stage not in {'task_text','board_url'}:
                await h.send('https://ctf.example/challenges')
            if stage in {'login','password','scope','submit'}:
                await h.click('access_password:1')
            elif stage=='token':await h.click('access_token:1')
            if stage in {'password','scope','submit'}:await h.send(login)
            if stage in {'scope','submit'}:await h.send(secret)
            if stage=='submit':await h.send('Only fixture challenge 1')
            assert h.store.wizard()['stage']==stage
            await h.send('/cancel')
            h.store.close();h.store=Store(path)
            h.dp=dispatcher(h.store,h.backend,123)
            if open_method=='picker':
                await h.send('/sessions');await h.click('job:1')
            else:await h.send('/status')
            assert h.store.wizard()['stage']==stage
            message=h.sent[-1]
            assert question in message.text
            assert login not in message.text and secret not in message.text
            controls=[b.callback_data for row in message.reply_markup.inline_keyboard for b in row]
            assert all(button in controls for button in buttons)
            assert 'start:1' not in controls
            h.backend.intake.assert_not_awaited()
        finally:await h.close()
    asyncio.run(run())


@pytest.mark.parametrize(('stage','question'), [
    ('board_url','адрес CTFd'),('access','Как входить'),('login','логин'),
    ('password','пароль'),('token','API-токен'),('scope','Какие задачи'),('submit','отправка'),
])
def test_file_after_cancel_and_restart_does_not_skip_unfinished_board_steps(tmp_path,stage,question):
    async def run():
        path=tmp_path/'state.db'
        h=IntakeHarness(path)
        login='fixture-private-login'
        secret='fixture-private-access-secret'
        try:
            await h.send('/board')
            if stage!='board_url':await h.send('https://ctf.example/challenges')
            if stage in {'login','password','scope','submit'}:await h.click('access_password:1')
            elif stage=='token':await h.click('access_token:1')
            if stage in {'password','scope','submit'}:await h.send(login)
            if stage in {'scope','submit'}:await h.send(secret)
            if stage=='submit':await h.send('Only fixture challenge 1')
            assert h.store.wizard()['stage']==stage
            before=h.store.session(h.store.current())['details']
            saved_access_count=h.backend.save_access.await_count
            await h.send('/cancel')
            h.store.close();h.store=Store(path)
            h.dp=dispatcher(h.store,h.backend,123)
            document=Document(file_id='fixture_file',file_unique_id='fixture_unique',file_name='fixture.txt',file_size=5)
            await h.send(document=document,caption='Do not skip the unfinished access form')
            row=h.store.session(h.store.current())
            assert row['stage']==stage and h.store.wizard()['stage']==stage
            assert row['details']==before
            assert json.loads(row['files'])==[]
            h.bot.download.assert_not_awaited()
            h.backend.upload.assert_not_awaited()
            h.backend.intake.assert_not_awaited()
            assert h.backend.save_access.await_count==saved_access_count
            message=h.sent[-1]
            assert question in message.text
            assert login not in message.text and secret not in message.text
            assert 'start:1' not in [b.callback_data for row in message.reply_markup.inline_keyboard for b in row]
        finally:await h.close()
    asyncio.run(run())


def test_old_card_cannot_start_another_current_draft(tmp_path):
    async def run():
        h = IntakeHarness(tmp_path / 'state.db')
        try:
            await h.send('/new')
            await h.send('Old draft\nFirst condition')
            await h.send('/new')
            await h.send('Current draft\nSecond condition')
            await h.click('start:1')
            h.backend.intake.assert_not_awaited()
            assert h.store.wizard()['sid'] == h.store.numbered(2)['id']
            assert h.store.latest(h.store.numbered(1)['id']) is None
            assert h.store.latest(h.store.numbered(2)['id']) is None
            assert 'не активна' in h.texts
        finally:
            await h.close()
    asyncio.run(run())


def test_short_use_and_active_status_never_show_backend_id(tmp_path):
    async def run():
        h = IntakeHarness(tmp_path / 'state.db')
        try:
            h.store.add_session('ses_hidden_original', title='Named original')
            h.store.prepare(100, 'ses_hidden_original', 'private fixture prompt')
            h.store.update(100, state='running')
            h.store.add_session('ses_hidden_other', title='Other')
            await h.send('/use #1')
            assert h.store.current() == 'ses_hidden_original'
            await h.send('/status')
            assert '#1 · Named original' in h.texts and 'Агент работает' in h.texts
            assert 'ses_hidden_' not in h.texts
            assert 'private fixture prompt' not in h.texts
        finally:
            await h.close()
    asyncio.run(run())


def test_document_upload_transfers_bytes_without_execution(tmp_path):
    async def run():
        h = IntakeHarness(tmp_path / 'state.db')
        try:
            await h.send('/new')
            document = Document(file_id='fixture_file', file_unique_id='fixture_unique',
                                file_name='sample.py', file_size=len(h.download_bytes))
            await h.send(document=document, caption='Uploaded task\nInspect this as data.')
            assert h.uploads == [h.download_bytes]
            h.backend.upload.assert_awaited_once()
            row = h.store.session(h.store.current())
            saved = json.loads(row['files'])
            assert len(saved) == 1 and saved[0]['name'] == 'sample.py'
            assert row['title'] == 'Uploaded task'
            assert h.store.wizard()['stage'] == 'files'
            await h.click('donefiles:1')
            await h.click('start:1')
            h.backend.intake.assert_awaited_once()
        finally:
            await h.close()
    asyncio.run(run())


@pytest.mark.parametrize('lie_about_size', [False, True])
def test_oversize_upload_is_rejected_before_remote_storage(tmp_path, lie_about_size):
    async def run():
        h = IntakeHarness(tmp_path / 'state.db')
        try:
            await h.send('/new')
            if lie_about_size:
                h.download_bytes = b'x' * (MAX_FILE + 1)
            document = Document(file_id='oversized_file', file_unique_id='oversized_unique',
                                file_name='large.bin', file_size=1 if lie_about_size else MAX_FILE + 1)
            await h.send(document=document)
            h.backend.upload.assert_not_awaited()
            assert json.loads(h.store.session(h.store.current())['files']) == []
            if not lie_about_size:
                h.bot.download.assert_not_awaited()
                assert '8 МиБ' in h.texts
        finally:
            await h.close()
    asyncio.run(run())
