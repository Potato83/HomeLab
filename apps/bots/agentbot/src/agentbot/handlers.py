"""Native Telegram task cards and persistent intake; private operations stay remote."""
import contextlib
import json
from pathlib import Path
import re
import tempfile
import uuid
from urllib.parse import urlsplit

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions

from . import ui
from .service import TERMINAL


MAX_FILE = 8 * 1024 * 1024


def board_url(text):
    value = text.strip().rstrip('/')
    p = urlsplit(value)
    if p.scheme not in {'http','https'} or not p.hostname or p.username or p.password or p.query or p.fragment or any(c.isspace() for c in value):
        raise ValueError('URL must not contain credentials')
    p.port
    return value


def install_handlers(dp, store, backend, owner):
    def wizard_keys(step, sid):
        return ui.wizard_keyboard(step, number=store.session(sid)['number'])

    async def respond(message, text, keyboard=None):
        for offset in range(0, len(text), 3500):
            await message.answer(text[offset:offset+3500], parse_mode=None,
                                 link_preview_options=LinkPreviewOptions(is_disabled=True),
                                 reply_markup=keyboard if offset+3500>=len(text) else None)

    def details(sid):
        return json.loads(store.session(sid)['details'])

    async def card(message, sid):
        row=store.session(sid);run=store.latest(sid);data=details(sid)
        questions={
            'task_text':'Пришли название и условие одним сообщением: первая строка станет названием. Можно сразу отправить файл с подписью.',
            'board_url':'Пришли адрес CTFd-борды (http/https), без логина и пароля в ссылке.',
            'access':'Как входить на борду? Доступ хранится отдельно от условий и не передаётся в текст промпта.',
            'login':'Пришли логин отдельного CTF-аккаунта.',
            'password':'Пришли пароль отдельным сообщением. После сохранения на Kali я удалю его из чата.',
            'token':'Пришли API-токен отдельным сообщением. После сохранения я удалю это сообщение.',
            'submit':'Разрешена автоматическая отправка проверенных флагов?',
        }
        if row['stage']=='scope':
            await ask_scope(message,sid,data);return
        if row['stage'] in questions:
            store.resume_wizard(sid)
            await respond(message,ui.task_label(row)+'\n'+questions[row['stage']],wizard_keys(row['stage'],sid));return
        draft=row['stage']!='active';running=run is not None and run['state'] not in TERMINAL
        access_saved=data.get('access_saved',row['stage'] in {'scope','submit','review','files','active'})
        await respond(message,ui.card_text(row,run,files_count=len(json.loads(row['files'])),
                      board_url=data.get('board_url'),draft=draft,access_mode=data.get('access_mode') if access_saved else None,allow_submit=data.get('allow_submit')),
                      ui.card_keyboard(row,draft=draft,running=running))

    async def listing(message, page=0):
        store.set_wizard()
        rows=store.sessions();current=store.session(store.current())
        await respond(message,'Мои задачи\nНажми на название, чтобы открыть.' if rows else 'Задач пока нет. Нажми «➕ Задача» или «🌐 Борда».',
                      ui.sessions_keyboard(rows,page,current['number'] if current else None))

    async def begin(message, kind, title=None):
        sid=await backend.create()
        store.add_session(sid,title=title or ('Новая борда' if kind=='board' else 'Новая задача'),kind=kind)
        stage='board_url' if kind=='board' else 'task_text'
        store.set_wizard(sid,stage,{'kind':kind,'title':store.session(sid)['title'],'allow_submit':False})
        text=('Пришли адрес CTFd-борды (http/https), без логина и пароля в ссылке.' if kind=='board' else
              'Пришли название и условие одним сообщением: первая строка станет названием. Можно сразу отправить файл с подписью.')
        await respond(message,text,wizard_keys(stage,sid))

    async def ask_scope(message, sid, data, update_id=None):
        store.set_wizard(sid,'scope',data,update_id=update_id)
        keyboard=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text='Все задачи этой борды',callback_data=f'scope_all:{store.session(sid)["number"]}')],
            [InlineKeyboardButton(text='Отмена',callback_data=f'cancel:{store.session(sid)["number"]}')]])
        await respond(message,'Какие задачи разрешено решать? Нажми «Все задачи» или напиши категории/номера и ограничения целей.',keyboard)

    async def preview(message, sid, data, update_id=None):
        store.set_wizard(sid,'review',data,update_id=update_id)
        await card(message,sid)

    async def stop(message, sid):
        row=store.latest(sid)
        if row and row['state'] not in TERMINAL:
            store.update(row['update_id'],stop_requested=1)
            await respond(message,f'{ui.task_label(store.session(sid))}\nОстановка запрошена. Подтверждение придёт от агента.',ui.menu_keyboard())
        else:
            await respond(message,'В этой задаче сейчас нет активной работы.',ui.menu_keyboard())

    async def result(message, sid):
        run=store.latest(sid)
        await respond(message,ui.task_label(store.session(sid))+'\n'+(run['answer'] if run and run['answer'] else 'Ответ ещё не получен.'),
                      ui.card_keyboard(store.session(sid),running=bool(run and run['state'] not in TERMINAL)))

    async def start(message, update_id):
        wizard=store.wizard()
        if not wizard or wizard['stage'] not in {'review','files'}:
            await respond(message,'Сначала закончи заполнение задачи. Меню — /start.',ui.menu_keyboard());return
        sid=wizard['sid'];data=wizard['payload'];row=store.session(sid)
        if not data.get('description') and not json.loads(row['files']) and row['kind']!='board':
            await respond(message,'Добавь условие или хотя бы один файл.');return
        if row['kind']=='board' and not data.get('scope'):
            await ask_scope(message,sid,data);return
        payload={k:v for k,v in data.items() if k in {'kind','title','description','board_url','scope','allow_submit'}}
        payload['title']=row['title']
        configured=await backend.intake(sid,payload)
        prompt=configured['prompt']
        if not isinstance(prompt,str) or not prompt:
            raise ValueError('Invalid intake response')
        if store.prepare(update_id,sid,prompt,clear_draft=True):
            await respond(message,ui.task_label(row)+'\n▶️ Задача сохранена и передаётся агенту. Можно продолжить диалог здесь.',ui.menu_keyboard())
        else:await respond(message,'Эта задача уже сохранена.',ui.menu_keyboard())

    async def attach(message, update_id=None):
        wizard=store.wizard();sid=wizard['sid'] if wizard else store.current()
        if not sid:
            await begin(message,'task');wizard=store.wizard();sid=wizard['sid']
        run=store.latest(sid)
        if run and run['state'] not in TERMINAL:
            await respond(message,'Сначала останови текущую работу или создай новую задачу для файла.',ui.menu_keyboard());return
        row=store.session(sid)
        if not wizard:
            if row['stage']!='active':
                store.resume_wizard(sid);wizard=store.wizard()
            else:
                data=details(sid);store.set_wizard(sid,'files',data);wizard=store.wizard()
        if wizard['stage'] not in {'files','review','task_text'}:
            if row['stage']!='active':await card(message,sid)
            else:await respond(message,'Сначала закончи текущий шаг, затем добавь файл.')
            return
        data=wizard['payload'];doc=message.document or (message.photo[-1] if message.photo else None)
        if doc is None:return
        if (doc.file_size or 0)>MAX_FILE:
            await respond(message,'Лимит одного файла — 8 МиБ. Архив можно разделить на части.');return
        name=getattr(doc,'file_name',None) or 'photo.jpg'
        upload_id=uuid.uuid5(uuid.NAMESPACE_URL,f'{sid}:{doc.file_unique_id}:{name}').hex
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'attachment'
            class BoundedFile:
                def __init__(self,file):self.file=file;self.size=0
                def write(self,chunk):
                    self.size+=len(chunk)
                    if self.size>MAX_FILE:raise ValueError('File too large')
                    return self.file.write(chunk)
                def seek(self,*args):return self.file.seek(*args)
                def flush(self):return self.file.flush()
            with path.open('wb') as file:
                await message.bot.download(doc.file_id,destination=BoundedFile(file))
            uploaded=await backend.upload(sid,path,name,upload_id=upload_id)
            if uploaded.get('complete') is not True:raise ValueError('Upload incomplete')
        store.add_file(sid,{k:uploaded[k] for k in ['name','path','size','upload_id']})
        if message.caption:
            data['description']=(data.get('description','')+'\n'+message.caption).strip()
        if wizard['stage']=='task_text':
            data['title']=(message.caption or name).splitlines()[0][:120]
            store.edit_session(sid,title=data['title'])
        store.set_wizard(sid,'files',data,update_id=update_id)
        await respond(message,ui.task_label(store.session(sid))+'\n📎 Файл сохранён на машине агента. Можно добавить ещё или закончить.',wizard_keys('files',sid))

    @dp.callback_query()
    async def callback(query,event_update):
        with contextlib.suppress(Exception):await query.answer()
        message=query.message
        if message is None:return
        try:
            action=query.data or ''
            wizard_actions={'start','addfiles','cancel','donefiles','access_password','access_token','access_none','scope_all','submit_yes','submit_no'}
            verb,sep,number=action.partition(':')
            if verb in wizard_actions and sep:
                wizard=store.wizard()
                if not number.isdigit() or not wizard or store.session(wizard['sid'])['number']!=int(number):
                    await respond(message,'Эта карточка не активна. Открой нужную задачу в «Мои задачи».',ui.menu_keyboard());return
                action=verb
            if action in {'cancel','menu'}:
                store.set_wizard();await respond(message,'Меню. Черновики и файлы сохранены.',ui.menu_keyboard());return
            if action.startswith('list:'):
                await listing(message,int(action.split(':',1)[1]));return
            if re.fullmatch(r'(job|status|result|stop|rename):\d+',action):
                verb,number=action.split(':');row=store.numbered(int(number))
                if row is None:await respond(message,'Задача не найдена. Открой «Мои задачи».');return
                sid=row['id'];store.select(sid)
                if verb in {'job','status'}:
                    store.set_wizard();store.resume_wizard(sid);await card(message,sid)
                elif verb=='result':await result(message,sid)
                elif verb=='stop':await stop(message,sid)
                else:
                    store.set_wizard(sid,'rename',{'previous_stage':row['stage'],'previous_details':details(sid)})
                    await respond(message,'Пришли новое короткое название.',wizard_keys('rename',sid))
                return
            wizard=store.wizard()
            if not wizard:await respond(message,'Открой нужную задачу в «Мои задачи».',ui.menu_keyboard());return
            sid=wizard['sid'];data=wizard['payload']
            if action=='start':await start(message,event_update.update_id)
            elif action=='addfiles':
                if wizard['stage'] not in {'review','files'}:await respond(message,'Сначала закончи текущий шаг.');return
                store.set_wizard(sid,'files',data);await respond(message,'Пришли файлы или фото. Для форензики лучше отправлять как файл — Telegram сжимает фото.',wizard_keys('files',sid))
            elif action=='donefiles' and wizard['stage']=='files':await preview(message,sid,data)
            elif action.startswith('access_') and wizard['stage']=='access':
                mode=action.removeprefix('access_')
                if mode not in {'none','password','token'}:return
                data['access_mode']=mode;data['access_saved']=False
                if mode=='none':
                    await backend.save_access(sid,{'mode':'none'});data['access_saved']=True;await ask_scope(message,sid,data)
                else:
                    step='login' if mode=='password' else 'token';store.set_wizard(sid,step,data)
                    await respond(message,'Пришли логин отдельного CTF-аккаунта.' if step=='login' else 'Пришли API-токен отдельным сообщением. После сохранения я удалю это сообщение.',wizard_keys(step,sid))
            elif action=='scope_all' and wizard['stage']=='scope':
                data['scope']='Разрешены задачи указанной CTF-борды и цели, явно указанные в их условиях; посторонние системы не трогать.'
                store.set_wizard(sid,'submit',data);await respond(message,'Разрешена автоматическая отправка проверенных флагов на эту борду?',wizard_keys('submit',sid))
            elif action in {'submit_yes','submit_no'} and wizard['stage']=='submit':
                data['allow_submit']=action=='submit_yes';await preview(message,sid,data)
        except Exception:
            with contextlib.suppress(Exception):await respond(message,'Действие не подтверждено. Черновик сохранён; повтори действие или открой «Мои задачи».',ui.menu_keyboard())

    @dp.message()
    async def receive(message,event_update):
        text=message.text or '';command,_,arg=text.partition(' ');command=command.split('@',1)[0]
        try:
            # A replayed password must never become the next step's scope/condition.
            if store.wizard_handled(event_update.update_id):
                wizard=store.wizard()
                if wizard and wizard['stage']=='scope':await ask_scope(message,wizard['sid'],wizard['payload'])
                elif wizard and wizard['stage']=='password':await respond(message,'Логин уже сохранён. Пришли пароль отдельным сообщением.',wizard_keys('password',wizard['sid']))
                elif wizard and wizard['stage'] in {'review','files'}:await card(message,wizard['sid'])
                elif wizard and wizard['stage']=='access':await respond(message,'Выбери способ доступа к борде.',wizard_keys('access',wizard['sid']))
                elif wizard and wizard['stage']=='submit':await respond(message,'Разрешена отправка проверенных флагов?',wizard_keys('submit',wizard['sid']))
                else:await respond(message,'Этот шаг уже сохранён.',ui.menu_keyboard())
                return
            if command in {'/start','/help',ui.HELP} or text==ui.HELP:
                store.set_wizard()
                await respond(message,ui.HELP_TEXT,ui.menu_keyboard());return
            if command=='/cancel':store.set_wizard();await respond(message,'Черновик сохранён. Меню:',ui.menu_keyboard());return
            if command=='/new' or text==ui.NEW_TASK:await begin(message,'task',arg.strip() or None);return
            if command=='/board' or text==ui.NEW_BOARD:await begin(message,'board');return
            if command=='/sessions' or text==ui.MY_TASKS:await listing(message);return
            if command=='/use':
                row=store.numbered(int(arg.strip().lstrip('#'))) if arg.strip().lstrip('#').isdigit() else store.session(arg.strip())
                if not row:await respond(message,'Нажми на задачу в «Мои задачи».',ui.sessions_keyboard(store.sessions()));return
                store.select(row['id']);store.set_wizard();store.resume_wizard(row['id']);await card(message,row['id']);return
            sid=store.current()
            if command in {'/status','/result','/stop'} or text in {ui.STATUS,ui.STOP}:
                if not sid:await respond(message,'Сначала выбери или создай задачу.',ui.menu_keyboard());return
                if command=='/stop' or text==ui.STOP:await stop(message,sid)
                elif command=='/result':await result(message,sid)
                else:await card(message,sid)
                return
            if text.startswith('/'):await respond(message,ui.HELP_TEXT,ui.menu_keyboard());return
            if message.document or message.photo:
                try:await attach(message,event_update.update_id)
                except Exception:await respond(message,'Загрузка не подтверждена. Перешли файл ещё раз: повторная передача не создаст дубль.',ui.menu_keyboard())
                return
            wizard=store.wizard()
            if wizard:
                sid=wizard['sid'];data=wizard['payload'];stage=wizard['stage']
                if not text.strip():await respond(message,'На этом шаге нужен текст.');return
                if stage=='task_text':
                    title=text.strip().splitlines()[0][:120];data.update(title=title,description=text.strip());store.edit_session(sid,title=title)
                    await preview(message,sid,data,event_update.update_id)
                elif stage=='board_url':
                    try:url=board_url(text)
                    except ValueError:await respond(message,'Нужен адрес http/https без логина, пароля и токена в ссылке.');return
                    title=urlsplit(url).hostname;data.update(board_url=url,title=title);store.edit_session(sid,title=title)
                    store.set_wizard(sid,'access',data,update_id=event_update.update_id);await respond(message,'Как входить на борду? Доступ хранится отдельно от условий и не передаётся в текст промпта.',wizard_keys('access',sid))
                elif stage=='login':
                    data['login']=text.strip();store.set_wizard(sid,'password',data,update_id=event_update.update_id)
                    with contextlib.suppress(Exception):await message.delete()
                    await respond(message,'Пришли пароль отдельным сообщением. После сохранения на Kali я удалю его из чата.',wizard_keys('password',sid))
                elif stage in {'password','token'}:
                    access={'mode':data['access_mode']}
                    if stage=='password':access.update(login=data.get('login',''),password=text)
                    else:access['token']=text.strip()
                    await backend.save_access(sid,access)
                    data['access_saved']=True
                    data.pop('login',None)
                    with contextlib.suppress(Exception):await message.delete()
                    await ask_scope(message,sid,data,event_update.update_id)
                elif stage=='scope':
                    data['scope']=text.strip();store.set_wizard(sid,'submit',data,update_id=event_update.update_id)
                    await respond(message,'Разрешена автоматическая отправка проверенных флагов?',wizard_keys('submit',sid))
                elif stage=='rename':
                    title=' '.join(text.split())[:120];store.edit_session(sid,title=title)
                    if data['previous_stage']!='active':
                        old=data['previous_details'];old['title']=title;store.set_wizard(sid,data['previous_stage'],old,update_id=event_update.update_id)
                    else:store.set_wizard(update_id=event_update.update_id)
                    await card(message,sid)
                elif stage in {'review','files'}:
                    if store.session(sid)['kind']=='board':
                        await respond(message,'Доступ уже сохранён. Нажми «Начать» или добавь файлы — пароль/токен повторять не нужно.',wizard_keys('review',sid));return
                    description=(data.get('description','')+'\n'+text).strip()
                    if len(description)>20000:await respond(message,'Условие больше 20 000 символов. Пришли дополнение текстовым файлом.');return
                    data['description']=description;await preview(message,sid,data,event_update.update_id)
                else:await respond(message,'Выбери вариант кнопкой выше или нажми «Отмена».')
                return
            if not text.strip():await respond(message,'Пришли текст, документ или фото.',ui.menu_keyboard());return
            if not sid:
                await begin(message,'task');wizard=store.wizard();sid=wizard['sid'];data=wizard['payload']
                title=text.strip().splitlines()[0][:120];data.update(title=title,description=text.strip());store.edit_session(sid,title=title);await preview(message,sid,data,event_update.update_id);return
            if store.session(sid)['stage']!='active':
                await respond(message,'Это незавершённый черновик. Открой его через «Мои задачи» или создай новую задачу.',ui.menu_keyboard());return
            if store.prepare(event_update.update_id,sid,text):
                await respond(message,ui.task_label(store.session(sid))+'\nСообщение сохранено. Агент продолжит работу.',ui.menu_keyboard())
            else:await respond(message,'Это сообщение уже сохранено.',ui.menu_keyboard())
        except ValueError:
            await respond(message,'В этой задаче уже есть работа или введены неверные данные. Открой статус либо создай новую задачу.',ui.menu_keyboard())
        except Exception:
            with contextlib.suppress(Exception):await respond(message,'Операция не подтверждена. Данные шага сохранены, кроме несохранённого пароля/токена. Повтори шаг после восстановления связи.',ui.menu_keyboard())
