import json
import time
from dataclasses import asdict
from .background import BackgroundScan
from .config import validate_config, words
from .http import FetchError
from .service import apply_result
from .reporting import status_text
from .telegram import deliver, format_message

HELP = (
    '/jobs [слово] — последние 5 вакансий\n'
    '/events [слово] — последние 5 событий\n'
    '/keywords — слова поиска\n'
    '/keywords SOC, DevOps, CTF — заменить слова\n'
    '/exclude слово, фраза — исключить; /exclude - — очистить\n'
    '/check — обновить источники\n/status — ошибки и результаты источников\n'
    'Новые совпадения приходят автоматически. Поиск без учёта регистра; '
    'длинные слова можно задавать частью слова. Несколько слов разделяются запятыми.'
)

class Monitor:
    def __init__(self, config, store, telegram, *, clock=time.monotonic, delivery_delay=0.2):
        self.config, self.store, self.telegram, self.clock = config, store, telegram, clock
        self.delivery_delay = delivery_delay
        self.revision, self.background = 0, None
        self.deadlines = {}
        saved = store.get_state('keywords_v2')
        if saved:
            self.config['keywords'] = json.loads(saved)
            validate_config(self.config)
        store.refilter(self.config['filters'])

    @property
    def enabled(self):
        return [n for n, s in self.config['sources'].items() if s.get('enabled')]

    def request_check(self, force=True):
        if self.background:
            return False
        due = [n for n in self.enabled if force or self.clock() >= self.deadlines.get(n, 0)]
        if not due:
            return False
        self.background = BackgroundScan(self.config, self.revision, due)
        return True

    def run_background(self):
        if self.background:
            completed = self.background.poll()
            if completed is None:
                return
            worker, self.background = self.background, None
            result, error = completed
            if worker.revision != self.revision:
                return  # The owner changed keywords during this scan.
            for name in worker.due:
                source = self.config['sources'][name]
                self.deadlines[name] = self.clock() + source.get('interval_seconds', self.config['monitor']['interval_seconds'])
            if error:
                for name in worker.due:
                    self.store.source_error(name, error)
            else:
                apply_result(self.store, result)
                self.dispatch_pending()
        self.request_check(force=False)

    def dispatch_pending(self):
        return deliver(self.store, self.telegram, self.enabled,
                       self.config['monitor']['max_messages_per_cycle'],
                       delay=self.delivery_delay, rules=self.config['filters'])

    def set_words(self, field, value):
        updated = dict(self.config['keywords'])
        updated[field] = words(value, allow_empty=field == 'exclude')
        self.config['keywords'] = updated
        validate_config(self.config)
        self.store.set_state('keywords_v2', json.dumps(updated, ensure_ascii=False))
        self.store.refilter(self.config['filters'])
        self.revision += 1
        self.deadlines.clear()
        if self.background:
            self.background.close()

    def handle(self, update):
        command = self.telegram.command(update)
        if not command:
            return
        args = self.telegram.arguments(update)
        if command in ('/start', '/help', '/menu'):
            self.telegram.send(HELP, reply_markup={'remove_keyboard': True})
        elif command in ('/keywords', '/exclude'):
            field = 'include' if command == '/keywords' else 'exclude'
            if args:
                self.set_words(field, [] if field == 'exclude' and args == '-' else args.split(','))
            from html import escape
            self.telegram.send(escape('Поиск: ' + ', '.join(self.config['keywords']['include']) +
                                     '\nИсключения: ' + (', '.join(self.config['keywords']['exclude']) or 'нет')))
        elif command in ('/jobs', '/events'):
            kind = 'event' if command == '/events' else 'vacancy'
            rows = self.store.search(kind, self.config['filters'], self.enabled, args)
            if not rows:
                self.telegram.send('Совпадений в последней проверке нет. Обновить: /check')
            for row in rows:
                self.telegram.send(format_message(row))
        elif command == '/status':
            self.telegram.send(('Проверка выполняется.\n' if self.background else '') + status_text(self.store, self.config))
        elif command == '/check':
            previous = float(self.store.get_state('manual_check_at', '0'))
            if time.time() - previous < 60:
                self.telegram.send('Повторная проверка доступна через минуту.')
            else:
                started = self.request_check()
                if started:
                    self.store.set_state('manual_check_at', time.time())
                self.telegram.send('Проверка запущена. Результаты придут автоматически.' if started else 'Проверка уже выполняется.')
        else:
            self.telegram.send('Справка: /help')

    def close(self):
        if self.background:
            self.background.close()
            self.background.thread.join(timeout=2)
