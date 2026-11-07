import asyncio
import time

from .store import Store


TERMINAL = {'done', 'error', 'stopped'}
LABELS = {'prepared': 'сохранено, ожидает отправки', 'submitting': 'отправляется',
          'unknown': 'отправка не подтверждена; автоматического повтора нет',
          'running': 'агент работает', 'done': 'завершено', 'error': 'ошибка агента',
          'stopped': 'остановлено'}


def completion(messages, mid, status):
    """Only consume this turn's completed reply, never an earlier session answer."""
    replies = [m for m in messages if m.get('info', {}).get('role') == 'assistant'
               and m['info'].get('parentID') == mid]
    if status.get('type', 'idle') != 'idle' or not replies:
        return None
    last = replies[-1]['info']
    if last.get('error'):
        return 'error', 'Агент сообщил об ошибке. Подробности доступны в закрытой сессии OpenCode.'
    if not last.get('time', {}).get('completed') or last.get('finish') == 'tool-calls':
        return None
    texts = [p.get('text', '') for m in replies for p in m.get('parts', [])
             if p.get('type') == 'text' and not p.get('ignored')]
    return 'done', '\n'.join(texts).strip() or 'Завершено без текстового ответа. Проверьте сессию OpenCode.'


class Service:
    def __init__(self, store: Store, backend, send, poll=5, send_task=None):
        self.store, self.backend, self.send, self.poll = store, backend, send, poll
        self.send_task = send_task
        self.last_notice = {}
        self.offline = False

    async def notify(self, sid, text):
        if self.send_task is not None:
            await self.send_task(sid, text)
        else:
            await self.send(text)

    async def deliver(self, row):
        # Cursor survives restart; a crash between Telegram acceptance and commit can repeat one chunk.
        text = row['answer']
        for start in range(row['sent'], len(text), 3500):
            await self.notify(row['session'],self.store.label(row['session'])+'\n'+text[start:start + 3500])
            self.store.update(row['update_id'], sent=min(start + 3500, len(text)))
        self.store.update(row['update_id'], notified=1)

    async def tick(self):
        rows = self.store.pending()
        statuses = None
        for row in rows:
            uid, sid = row['update_id'], row['session']
            if row['state'] in TERMINAL:
                await self.deliver(row)
                continue
            if row['stop_requested']:
                if row['state'] != 'prepared':
                    await self.backend.abort(sid, row['message'])
                self.store.update(uid, state='stopped', prompt=None, answer='Работа остановлена.')
                await self.deliver(self.store.get(uid))
                continue
            if row['state'] == 'prepared':
                self.store.update(uid, state='submitting')
                try:
                    await self.backend.submit(sid, row['message'], row['prompt'])
                except Exception:
                    self.store.update(uid, state='unknown')
                    raise
                self.store.update(uid, state='running', prompt=None)
                continue
            if statuses is None:
                statuses = await self.backend.statuses()
            messages = await self.backend.messages(sid)
            if any(m.get('info', {}).get('id') == row['message'] for m in messages):
                self.store.update(uid, state='running', prompt=None)
            result = completion(messages, row['message'], statuses.get(sid, {}))
            if result:
                self.store.update(uid, state=result[0], answer=result[1], prompt=None)
                await self.deliver(self.store.get(uid))
            elif time.monotonic() - self.last_notice.get(uid, 0) >= 60:
                current = self.store.get(uid)
                age = int(time.time() - current['created'])
                phase = statuses.get(sid, {}).get('type', 'idle')
                phase = phase if phase in {'busy', 'idle', 'retry'} else 'unknown'
                await self.notify(sid,f"{self.store.label(sid)}\n{LABELS[current['state']]}; {age} с.")
                self.last_notice[uid] = time.monotonic()

    async def watch(self):
        while True:
            try:
                await self.tick()
                if self.offline:
                    await self.send('Связь восстановлена. Проверка сохранённых задач продолжена.')
                self.offline = False
            except Exception:
                if not self.offline:
                    self.offline = True
                    try:
                        await self.send('Связь с OpenCode или Telegram прервана. Задачи сохранены; проверка повторяется. /status')
                    except Exception:
                        pass
            await asyncio.sleep(self.poll)
