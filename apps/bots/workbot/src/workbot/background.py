"""Network-only daemon worker. SQLite and Telegram remain on their owning thread."""
from copy import deepcopy
from queue import Queue, Empty
from threading import Thread, Event
from .http import HttpClient, RequestCancelled
from .service import scan


Cancelled = RequestCancelled


class BackgroundScan:
    def __init__(self, config, revision, due, collect=None):
        self.revision, self.due = revision, list(due)
        self.cancelled = Event()
        self.results = Queue(maxsize=1)
        snapshot = deepcopy(config)
        for name, settings in snapshot['sources'].items():
            settings['enabled'] = name in self.due
        def work():
            try:
                if collect is not None:
                    result = collect(snapshot)
                else:
                    m = snapshot['monitor']
                    result = scan(snapshot, client=HttpClient(
                        m['request_timeout_seconds'], m['request_delay_seconds'],
                        scan_timeout=m.get('scan_timeout_seconds', 600),
                        cancel_event=self.cancelled))
                self.results.put((result, None))
            except Cancelled:
                self.results.put((None, 'cancelled'))
            except Exception:
                # No arbitrary network exception text (which may contain credentials).
                self.results.put((None, 'Фоновая проверка завершилась с ошибкой; подробности источников недоступны'))
        self.thread = Thread(target=work, name='workbot-scan', daemon=True)
        self.thread.start()

    def poll(self):
        try:
            return self.results.get_nowait()
        except Empty:
            return None

    def close(self):
        self.cancelled.set()
