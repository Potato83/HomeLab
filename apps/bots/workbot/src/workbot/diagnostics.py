"""Startup diagnostics. Does not open Store or touch delivery history."""

from contextlib import closing
import os
from pathlib import Path
import sqlite3
import sys
import tempfile

from . import __version__
from .config import load_config
from .http import FetchError, telegram_call
from .reporting import NAMES
from .service import scan
from .telegram import Telegram, private_chat_id


def database_check(value):
    if not value or value == ':memory:':
        raise ValueError('Для постоянной работы задайте путь к файлу SQLite')
    path = Path(value).absolute()
    if path.exists():
        if not path.is_file():
            raise ValueError('Путь базы указывает не на файл')
        if path.stat().st_size:
            with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
                if db.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                    raise ValueError('Проверка целостности SQLite завершилась ошибкой')
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not {'vacancies', 'source_status', 'state'} <= tables:
                    raise ValueError('В файле отсутствуют основные таблицы WorkBot; проверьте WORKBOT_DB')
        # A readable file can still be unwritable; opening r+b does not modify it.
        with path.open('r+b'):
            pass
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.workbot-probe-', dir=path.parent) as directory:
        with closing(sqlite3.connect(str(Path(directory) / 'probe.sqlite3'))) as db:
            if db.execute('PRAGMA journal_mode=WAL').fetchone()[0] != 'wal':
                raise ValueError('Файловая система не поддерживает режим SQLite WAL')
            db.execute('CREATE TABLE probe(value INTEGER)')
            db.execute('INSERT INTO probe VALUES (1)')
            db.commit()
        # The runner also relies on advisory file locks on this filesystem.
        import fcntl
        with (Path(directory) / 'probe.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(lock, fcntl.LOCK_UN)
    return 'SQLite, запись, WAL и блокировка работают; история отправок не изменена'


def check_bot(token):
    result = telegram_call(token, 'getMe', {})
    if not isinstance(result, dict) or result.get('is_bot') is not True or type(result.get('id')) is not int:
        raise FetchError('Telegram: getMe вернул некорректные данные бота')
    return result


def check_private_chat(token, chat_id):
    chat_id = private_chat_id(chat_id)
    result = telegram_call(token, 'getChat', {'chat_id': chat_id})
    if not isinstance(result, dict) or result.get('type') != 'private' or str(result.get('id')) != chat_id:
        raise FetchError('Telegram: полученный чат не совпадает с настроенным личным чатом')
    return result


def diagnose(config_path=None, *, online=False, skip_sources=False, env=None, env_error=None):
    env = os.environ if env is None else env
    token, chat_id = env.get('BOT_TOKEN', ''), env.get('OWNER_ID', '')
    secrets = [value for value in (token, token.partition(':')[2]) if len(value) >= 6]
    checks = []
    def add(key, state, detail):
        for secret in secrets:
            detail = str(detail).replace(secret, '[скрыто]')
        checks.append({'check': key, 'state': state, 'detail': str(detail)})
    add('environment', 'error' if env_error else 'ok', env_error or '.env и переменные окружения прочитаны')
    add('python', 'ok' if sys.version_info >= (3, 11) else 'error',
        f'Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}; требуется 3.11+')
    config = None
    try:
        config = load_config(config_path or env.get('WORKBOT_CONFIG', 'config.toml'))
        count = sum(bool(item.get('enabled')) for item in config['sources'].values())
        add('config', 'ok', f'Конфигурация корректна; включено источников: {count}')
        if config['sources'].get('hh', {}).get('enabled'):
            from .hh_source import valid_token
            ready = valid_token(env.get('HH_API_TOKEN', ''))
            add('hh_api_token', 'ok' if ready else 'error',
                'HH_API_TOKEN задан; доступ проверяется с --online' if ready else 'Заполните HH_API_TOKEN в .env для включённого hh.ru')
    except (ValueError, KeyError, TypeError, AttributeError, OSError) as exc:
        add('config', 'error', f'Не удалось проверить конфигурацию: {exc}')
    try:
        add('database', 'ok', database_check(env.get('WORKBOT_DB', 'data/workbot.sqlite3')))
    except (ValueError, OSError, sqlite3.Error, ImportError) as exc:
        add('database', 'error', f'База или каталог недоступны для работы: {exc}')
    token_ready = bool(token) and not any(c.isspace() for c in token)
    add('telegram_token', 'ok' if token_ready else 'error',
        'Токен задан; подлинность проверяется только с --online' if token_ready else 'Заполните BOT_TOKEN в .env')
    chat_ready = False
    try:
        private_chat_id(chat_id)
        chat_ready = True
        add('telegram_chat', 'ok', 'Задан числовой ID личного чата')
    except ValueError as exc:
        add('telegram_chat', 'error', str(exc) + '; получить ID: python -m workbot chat-id')
    if online and token_ready:
        authenticated = False
        try:
            check_bot(token)
            authenticated = True
            add('telegram_auth', 'ok', 'Telegram подтвердил токен бота')
        except FetchError as exc:
            add('telegram_auth', 'error', str(exc))
        if authenticated:
            try:
                hook = telegram_call(token, 'getWebhookInfo', {})
                if not isinstance(hook, dict) or not isinstance(hook.get('url'), str):
                    raise FetchError('Telegram: некорректный ответ getWebhookInfo')
                if hook['url']:
                    add('telegram_polling', 'error', 'Для бота настроен webhook, несовместимый с long polling. Диагностика его не изменяет.')
                else:
                    add('telegram_polling', 'ok', 'Webhook отсутствует; конфликт webhook/long polling не обнаружен')
            except FetchError as exc:
                add('telegram_polling', 'error', str(exc))
            if chat_ready:
                try:
                    check_private_chat(token, chat_id)
                    add('telegram_access', 'ok', 'Личный чат найден; сообщение не отправлялось')
                except FetchError as exc:
                    add('telegram_access', 'error', str(exc) + '; напишите вашему боту /start и проверьте chat_id')
        else:
            add('telegram_access', 'skipped', 'Доступ к чату не проверен из-за ошибки токена')
    else:
        add('telegram_network', 'skipped', 'Telegram по сети не проверялся' if not online else 'Для проверки Telegram нужен токен')
    if online and not skip_sources and config:
        result = scan(config)  # No Store: no snapshots, offsets or notification acknowledgements.
        for name, state in result['sources'].items():
            detail = state.get('error') or f'Получено: {state["found"]}; подходит: {state["matched"]}'
            add('source:' + name, 'error' if state.get('error') else 'ok', NAMES.get(name, name) + ': ' + detail)
    else:
        add('sources_network', 'skipped', 'Источники по сети не проверялись')
    return {'version': __version__, 'mode': 'online' if online else 'offline',
            'ok': not any(item['state'] == 'error' for item in checks), 'checks': checks}


def format_report(report):
    labels = {'ok': 'OK', 'error': 'ОШИБКА', 'skipped': 'ПРОПУЩЕНО'}
    mode = 'сетевая' if report['mode'] == 'online' else 'локальная'
    lines = [f'WorkBot {report["version"]}: {mode} диагностика']
    lines += [f'[{labels[item["state"]]}] {item["check"]}: {item["detail"]}' for item in report['checks']]
    lines.append('Проверки выбранного режима пройдены.' if report['ok'] else 'Есть ошибки; исправьте отмеченные пункты перед запуском.')
    return '\n'.join(lines)


def send_test_message(token, chat_id):
    telegram = Telegram(token, chat_id)
    check_bot(token)
    check_private_chat(token, telegram.chat_id)
    telegram.send(f'✅ WorkBot {__version__}: тестовое уведомление.\nБот может отправлять сообщения в этот чат.')
    return {'ok': True, 'message': 'Telegram подтвердил отправку одного тестового сообщения в настроенный личный чат.'}
