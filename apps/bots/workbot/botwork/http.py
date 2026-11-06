import json
import socket
import time
import ssl
from threading import Event, Lock, Thread
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen, build_opener, HTTPRedirectHandler
from . import __version__
from .limits import (DEFAULT_SCAN_TIMEOUT_SECONDS, MAX_HTTP_BYTES_PER_SCAN,
                     MAX_HTTP_REQUESTS_PER_SCAN, MAX_HTTP_RESPONSE_BYTES)


class FetchError(RuntimeError):
    pass


class RequestCancelled(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HttpClient:
    def __init__(self, timeout=25, delay=0.5, *, scan_timeout=DEFAULT_SCAN_TIMEOUT_SECONDS,
                 cancel_event=None, max_requests=MAX_HTTP_REQUESTS_PER_SCAN,
                 max_bytes=MAX_HTTP_BYTES_PER_SCAN):
        self.timeout = timeout
        self.delay = delay
        self.last_request = 0.0
        self.deadline = time.monotonic() + scan_timeout
        self.cancel_event = cancel_event
        self.max_requests = max_requests
        self.max_bytes = max_bytes
        self.request_count = 0
        self.bytes_received = 0
        self._limits_lock = Lock()

    def get(self, url, *, headers=None, follow_redirects=False):
        for attempt in range(2):
            try:
                return self._get_once(url, headers=headers, follow_redirects=follow_redirects)
            except FetchError as exc:
                if attempt or str(exc) in ("HTTP 400", "HTTP 401", "HTTP 403", "HTTP 404", "HTTP 429"):
                    raise
                self._wait(2)

    def _check_active(self):
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise RequestCancelled()
        if time.monotonic() >= self.deadline:
            raise FetchError("Общий лимит времени проверки исчерпан")

    def _wait(self, seconds):
        self._check_active()
        duration = min(max(0, seconds), max(0, self.deadline - time.monotonic()))
        if self.cancel_event is None:
            time.sleep(duration)
        elif self.cancel_event.wait(duration):
            raise RequestCancelled()
        self._check_active()

    def _reserve_request(self):
        self._check_active()
        with self._limits_lock:
            if self.request_count >= self.max_requests:
                raise FetchError("Превышен общий лимит HTTP-запросов за проверку")
            self.request_count += 1

    @staticmethod
    def _response_socket(response):
        try:
            return response.fp.raw._sock
        except AttributeError:
            return None

    def _get_once(self, url, *, headers=None, follow_redirects=False):
        self._wait(max(0, self.delay - (time.monotonic() - self.last_request)))
        self._reserve_request()
        request_deadline = min(time.monotonic() + self.timeout, self.deadline)
        scan_limited = self.deadline <= request_deadline
        try:
            request_headers = {"User-Agent": f"BotWork/{__version__} (personal vacancy monitor)", "Accept": "text/html"}
            request_headers.update(headers or {})
            req = Request(url, headers=request_headers)
            opener = urlopen if follow_redirects else build_opener(NoRedirect()).open
            connect_timeout = request_deadline - time.monotonic()
            if connect_timeout <= 0:
                self._check_active()
                raise FetchError("Ошибка сети, TLS или таймаут")
            with opener(req, timeout=connect_timeout) as response:
                remaining = self.max_bytes - self.bytes_received
                if remaining <= 0:
                    raise FetchError("Превышен общий лимит объёма HTTP-ответов за проверку")
                read_limit = min(MAX_HTTP_RESPONSE_BYTES + 1, max(1, remaining + 1))
                stop_watch = Event()
                interrupt_reason = []
                response_socket = self._response_socket(response)
                watcher = None
                if response_socket is not None:
                    def interrupt_blocked_read():
                        while not stop_watch.is_set():
                            if self.cancel_event is not None and self.cancel_event.is_set():
                                interrupt_reason.append('cancelled')
                                try:
                                    response_socket.shutdown(socket.SHUT_RDWR)
                                except OSError:
                                    pass
                                return
                            left = request_deadline - time.monotonic()
                            if left <= 0:
                                interrupt_reason.append('scan-timeout' if scan_limited else 'request-timeout')
                                try:
                                    response_socket.shutdown(socket.SHUT_RDWR)
                                except OSError:
                                    pass
                                return
                            stop_watch.wait(min(left, 0.05))
                    watcher = Thread(target=interrupt_blocked_read, name='botwork-http-deadline', daemon=True)
                    watcher.start()
                try:
                    chunks, total = [], 0
                    while total < read_limit:
                        chunk = response.read(min(64_000, read_limit - total))
                        if not chunk:
                            break
                        chunks.append(chunk)
                        total += len(chunk)
                        with self._limits_lock:
                            self.bytes_received += len(chunk)
                            aggregate_bytes = self.bytes_received
                        if total > MAX_HTTP_RESPONSE_BYTES:
                            raise FetchError("Страница превышает лимит размера")
                        if aggregate_bytes > self.max_bytes:
                            raise FetchError("Превышен общий лимит объёма HTTP-ответов за проверку")
                    raw = b''.join(chunks)
                finally:
                    stop_watch.set()
                    if watcher is not None:
                        watcher.join(timeout=0.2)
                self._check_active()
                if interrupt_reason:
                    if interrupt_reason[0] == 'cancelled':
                        raise RequestCancelled()
                    if interrupt_reason[0] == 'scan-timeout':
                        raise FetchError("Общий лимит времени проверки исчерпан")
                    raise FetchError("Ошибка сети, TLS или таймаут")
                return raw.decode(response.headers.get_content_charset() or "utf-8")
        except HTTPError as exc:
            self._check_active()
            raise FetchError(f"HTTP {exc.code}") from None
        except URLError as exc:
            self._check_active()
            if isinstance(exc.reason, ssl.SSLCertVerificationError):
                raise FetchError('Ошибка проверки TLS-сертификата') from None
            raise FetchError("Ошибка сети, TLS или таймаут") from None
        except RequestCancelled:
            raise
        except (TimeoutError, OSError, UnicodeError):
            self._check_active()
            raise FetchError("Ошибка сети, TLS или таймаут") from None
        finally:
            self.last_request = time.monotonic()


def telegram_call(token, method, payload, timeout=30):
    # Never propagate urllib exceptions: their URL contains the bot token.
    try:
        req = Request(f"https://api.telegram.org/bot{token}/{method}",
                      data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
        with build_opener(NoRedirect()).open(req, timeout=timeout) as response:
            result = json.load(response)
    except HTTPError as exc:
        raise FetchError(f"Telegram HTTP {exc.code}") from None
    except (URLError, TimeoutError, OSError, ValueError, TypeError):
        raise FetchError("Telegram: ошибка сети или ответа") from None
    if not isinstance(result, dict) or type(result.get('ok')) is not bool:
        raise FetchError('Telegram: некорректный ответ API')
    if not result['ok']:
        code = result.get('error_code')
        raise FetchError(f"Telegram: отказ API ({code if type(code) is int else 'unknown'})")
    if 'result' not in result:
        raise FetchError('Telegram: отсутствует результат API')
    return result["result"]
