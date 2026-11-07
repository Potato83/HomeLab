"""Unprivileged client and short-lived, single-use action confirmations."""
import asyncio
import json
import secrets
import time
from dataclasses import dataclass

LABELS = {'kali': 'Kali', 'chatbot': 'ChatBot', 'workbot': 'WorkBot',
          'agentbot': 'AgentBot', 'omniroute': 'OmniRoute'}
STATES = {'running': 'Работает', 'stopped': 'Остановлен', 'unknown': 'Состояние неизвестно'}
ICONS = {'running': '🟢', 'stopped': '⚪', 'unknown': '🟡'}
ERRORS = {
    'unavailable': 'Управление недоступно. Проверь установку системного исполнителя.',
    'permission_denied': 'Нет доступа к системному исполнителю.',
    'busy': 'Для этого объекта уже выполняется операция.',
    'state_changed': 'Состояние изменилось. Обнови карточку и повтори действие.',
    'command_failed': 'Команда завершилась с ошибкой. Проверь состояние и последние ошибки.',
    'state_timeout': 'Не удалось подтвердить результат за отведённое время. Обнови состояние.',
    'logs_unavailable': 'Журнал пока недоступен.',
    'not_installed': 'Служба не установлена.',
    'invalid_action': 'Действие недоступно.',
}


class ControlError(Exception):
    pass


@dataclass(frozen=True)
class Target:
    target: str
    state: str
    manual: bool = False

    @classmethod
    def parse(cls, value):
        if (not isinstance(value, dict) or not isinstance(value.get('target'), str)
                or value['target'] not in LABELS or not isinstance(value.get('state'), str)
                or value['state'] not in STATES or type(value.get('manual')) is not bool):
            raise ValueError('invalid target')
        return cls(value['target'], value['state'], value['manual'])


@dataclass(frozen=True)
class Confirmation:
    user_id: int
    chat_id: int
    message_id: int
    target: str
    action: str
    expires_at: float


class ControlClient:
    def __init__(self, enabled=False):
        self.enabled = enabled
        self.pending: dict[str, Confirmation] = {}
        self.busy: set[str] = set()
        self.last_read: dict[int, float] = {}

    async def request(self, *args):
        if not self.enabled:
            raise ControlError('Управление ещё не включено на сервере.')
        # A fixed executable; neither configuration nor Telegram may supply a command/path.
        try:
            process = await asyncio.create_subprocess_exec(
                '/usr/bin/doas', '-n', '/usr/local/lib/homelab/control.py', *args,
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL)
            try:
                output, _ = await asyncio.wait_for(process.communicate(), timeout=180)
            except (TimeoutError, asyncio.CancelledError):
                # doas may already have switched UID; the privileged operation has its own deadline.
                try:
                    process.kill()
                except (ProcessLookupError, PermissionError):
                    pass
                raise
            if process.returncode or len(output) > 160000:
                raise ValueError('invalid helper response')
            payload = json.loads(output)
            if not isinstance(payload, dict) or type(payload.get('ok')) is not bool:
                raise ValueError('invalid helper response')
            if not payload['ok']:
                raise ControlError(ERRORS.get(payload.get('error'), ERRORS['unavailable']))
            return payload
        except (OSError, ValueError, TypeError):
            raise ControlError(ERRORS['unavailable']) from None
        except TimeoutError:
            raise ControlError(ERRORS['state_timeout']) from None

    async def targets(self):
        payload = await self.request('list')
        try:
            rows = payload['targets']
            if not isinstance(rows, list) or len(rows) > len(LABELS):
                raise ValueError('invalid list')
            targets = [Target.parse(row) for row in rows]
            if len({row.target for row in targets}) != len(targets):
                raise ValueError('duplicate targets')
            return targets
        except (ValueError, KeyError, TypeError):
            raise ControlError(ERRORS['unavailable']) from None

    async def status(self, target):
        if target not in LABELS:
            raise ControlError(ERRORS['invalid_action'])
        try:
            row = Target.parse(await self.request(target, 'status'))
            if row.target != target:
                raise ValueError('wrong target')
            return row
        except ValueError:
            raise ControlError(ERRORS['unavailable']) from None

    async def errors(self, target):
        if target not in LABELS or target == 'kali':
            raise ControlError(ERRORS['invalid_action'])
        text = (await self.request(target, 'errors')).get('text')
        if not isinstance(text, str) or len(text) > 13000:
            raise ControlError(ERRORS['unavailable'])
        return text

    def forget_message(self, chat_id, message_id):
        now = time.monotonic()
        self.pending = {token: item for token, item in self.pending.items()
                        if item.expires_at > now and (item.chat_id, item.message_id) != (chat_id, message_id)}

    def confirm(self, user_id, chat_id, message_id, target, action):
        self.forget_message(chat_id, message_id)
        if len(self.pending) >= 1000:
            raise ControlError('Слишком много ожидающих подтверждений. Повтори чуть позже.')
        token = secrets.token_urlsafe(12)
        self.pending[token] = Confirmation(user_id, chat_id, message_id, target, action, time.monotonic() + 60)
        return token

    def consume(self, token, user_id, chat_id, message_id):
        item = self.pending.get(token)
        if (item is None or item.expires_at < time.monotonic() or
                (item.user_id, item.chat_id, item.message_id) != (user_id, chat_id, message_id)):
            raise ControlError('Подтверждение истекло или уже использовано. Открой карточку заново.')
        del self.pending[token]
        return item

    async def execute(self, target, action):
        if target not in LABELS or action not in {'start', 'stop', 'restart'}:
            raise ControlError(ERRORS['invalid_action'])
        if target in self.busy:
            raise ControlError(ERRORS['busy'])
        self.busy.add(target)
        try:
            row = Target.parse(await self.request(target, action))
            expected = 'stopped' if action == 'stop' else 'running'
            if row.target != target or row.state != expected:
                raise ValueError('unconfirmed result')
            return row
        except ValueError:
            raise ControlError(ERRORS['state_timeout']) from None
        finally:
            self.busy.discard(target)

    def allow_read(self, user_id):
        now = time.monotonic()
        if now - self.last_read.get(user_id, -1) < 0.7:
            return False
        self.last_read[user_id] = now
        return True
