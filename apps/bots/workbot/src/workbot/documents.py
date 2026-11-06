"""Small Telegram JSON documents; token-bearing URLs never escape errors."""
import json
import re
import uuid
from urllib.request import Request, build_opener
from .http import FetchError, NoRedirect, telegram_call

LIMIT = 1_000_000


def send_json(token, chat_id, filename, content):
    raw = json.dumps(content, ensure_ascii=False, indent=2).encode('utf-8')
    if len(raw) > LIMIT or not re.fullmatch(r'[a-zA-Z0-9_.-]+\.json', filename):
        raise ValueError('Файл JSON должен быть не больше 1 МБ.')
    return send_file(token, chat_id, filename, raw, 'application/json')


def send_file(token, chat_id, filename, raw, mime):
    if len(raw) > LIMIT or not re.fullmatch(r'[a-zA-Z0-9_.-]+\.(json|csv)', filename) or mime not in ('application/json', 'text/csv'):
        raise ValueError('Файл должен быть JSON/CSV не больше 1 МБ.')
    boundary = 'WorkBot' + uuid.uuid4().hex
    body = ('--' + boundary + '\r\nContent-Disposition: form-data; name="chat_id"\r\n\r\n' + str(chat_id) + '\r\n'
        '--' + boundary + '\r\nContent-Disposition: form-data; name="document"; filename="' + filename + '"\r\n'
        'Content-Type: ' + mime + '\r\n\r\n').encode() + raw + ('\r\n--' + boundary + '--\r\n').encode()
    try:
        request = Request('https://api.telegram.org/bot' + token + '/sendDocument', data=body,
            headers={'Content-Type': 'multipart/form-data; boundary=' + boundary})
        with build_opener(NoRedirect()).open(request, timeout=30) as response:
            data = json.load(response)
        if not isinstance(data, dict) or data.get('ok') is not True or 'result' not in data:
            raise ValueError
        return data['result']
    except Exception:
        raise FetchError('Telegram: не удалось отправить файл') from None


def read_json(token, document):
    if not isinstance(document, dict) or not isinstance(document.get('file_id'), str):
        raise ValueError('Прикрепите JSON-файл с подписью /site_import.')
    size = document.get('file_size')
    if type(size) is not int or not 0 < size <= LIMIT:
        raise ValueError('Файл JSON должен быть не больше 1 МБ.')
    result = telegram_call(token, 'getFile', {'file_id': document['file_id']})
    path = result.get('file_path') if isinstance(result, dict) else None
    if not isinstance(path, str) or not re.fullmatch(r'[a-zA-Z0-9_./-]+', path) or '..' in path.split('/') or path.startswith('/'):
        raise FetchError('Telegram: неизвестный адрес файла')
    try:
        request = Request('https://api.telegram.org/file/bot' + token + '/' + path)
        with build_opener(NoRedirect()).open(request, timeout=30) as response:
            raw = response.read(LIMIT + 1)
    except Exception:
        raise FetchError('Telegram: не удалось прочитать JSON-файл') from None
    if len(raw) > LIMIT:
        raise ValueError('Файл JSON должен быть не больше 1 МБ.')
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError('Файл содержит некорректный JSON.') from None
