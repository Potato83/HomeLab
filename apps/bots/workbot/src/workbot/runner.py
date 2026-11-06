import logging
import time
from copy import deepcopy
from .settings import effective_config

from .health import deliver_health
from .http import FetchError
from .profile_matching import combined_collection_filters, decide_profiles
from .schedule import allows_scan
from .service import scan
from .storage import now
from .telegram import deliver, deliver_digest, deliver_event_cancellations

log = logging.getLogger(__name__)


class Monitor:
    def __init__(self, config, store, telegram, clock=None, delivery_delay=1.1):
        self.base_config, self.store, self.telegram = deepcopy(config), store, telegram
        self.config = effective_config(config, store)
        self.clock = clock or time.monotonic
        self.delivery_delay = delivery_delay
        self.next_scan = 0.0
        self.next_delivery = 0.0
        self.enabled = [name for name, settings in self.config['sources'].items() if settings.get('enabled')]
        self.source_due = {name: 0.0 for name in self.enabled}
        self.revision = 0
        self.background = None
        if telegram is not None:
            self.store.refilter(self.config)

    def reload_settings(self):
        updated = effective_config(self.base_config, self.store)
        if updated == self.config:
            return
        self.revision += 1
        if self.background:
            self.background.close()
        if any(updated['monitor'].get(key) != self.config['monitor'].get(key) for key in ('delivery_mode', 'digest_seconds')):
            self.store.set_state('digest_next_at', '')
        self.config = updated
        self.enabled = [name for name, settings in self.config['sources'].items() if settings.get('enabled')]
        self.source_due = {name: 0.0 for name in self.enabled}
        self.next_scan = 0.0
        self.store.refilter(self.config)

    def _dispatch(self):
        sent = {'alerts': 0, 'vacancies': 0}
        if self.config['monitor'].get('paused'):
            return sent
        # Calendar dates/deadlines can expire between network checks.
        self.store.refilter(self.config, events_only=True)
        errors = []
        try:
            sent['alerts'] = deliver_health(self.store, self.telegram, self.config, delay=self.delivery_delay)
        except FetchError as exc:
            errors.append(str(exc))
        # An alert delivery problem must not block vacancy delivery attempts.
        checking = set(self.background.due) if self.background else set()
        healthy = [row['source'] for row in self.store.status()
                   if row['source'] in self.enabled and row['error'] is None and row['source'] not in checking]
        try:
            settings = self.config['monitor']
            if settings.get('delivery_mode', 'instant') == 'digest':
                sent['vacancies'] = deliver_event_cancellations(
                    self.store, self.telegram, healthy, settings['max_messages_per_cycle'], delay=self.delivery_delay)
                next_at = self.store.get_state('digest_next_at')
                if not next_at:
                    self.store.set_state('digest_next_at', time.time() + settings.get('digest_seconds', 3600))
                elif time.time() >= float(next_at):
                    remaining = max(0, settings['max_messages_per_cycle'] - sent['vacancies'])
                    if remaining:
                        sent['vacancies'] += deliver_digest(self.store, self.telegram, healthy, remaining, delay=self.delivery_delay)
                    self.store.set_state('digest_next_at', time.time() + settings.get('digest_seconds', 3600))
            else:
                sent['vacancies'] = deliver_event_cancellations(
                    self.store, self.telegram, healthy, settings['max_messages_per_cycle'], delay=self.delivery_delay)
                remaining = max(0, settings['max_messages_per_cycle'] - sent['vacancies'])
                if remaining:
                    sent['vacancies'] += deliver(self.store, self.telegram, healthy, remaining, delay=self.delivery_delay)
        except FetchError as exc:
            errors.append(str(exc))
        if errors:
            self.store.set_state('last_delivery_error', '; '.join(errors))
            self.store.set_state('last_delivery_error_at', now())
            raise FetchError('; '.join(errors))
        if sum(sent.values()):
            self.store.set_state('last_delivery_error', '')
        return sent

    def request_check(self, force=True):
        from .background import BackgroundScan
        if self.background or self.config['monitor'].get('paused'):
            return False
        if not force and not allows_scan(self.config['monitor'].get('schedule', 'off')):
            self.next_scan = self.clock() + 60
            return False
        filters = combined_collection_filters(self.config)
        due = [name for name in self.enabled if (force or self.source_due.get(name, 0) <= self.clock())
               and (name not in ('ctftime', 'ods_events') or filters.get('events_enabled', False))]
        if not due:
            self.next_scan = self.clock() + self.config['monitor']['interval_seconds']
            return False
        snapshot = deepcopy(self.config)
        if filters.get('event_cancellations', True) or filters.get('event_updates', True):
            for name in due:
                if name in ('ctftime', 'ods_events'):
                    snapshot['sources'][name]['_watched_events'] = self.store.watched_events(name)
        self.background = BackgroundScan(snapshot, self.revision, due)
        return True

    def run_background(self):
        if self.background:
            finished = self.background.poll()
            if finished is not None:
                job, self.background = self.background, None
                result, error = finished
                if job.revision == self.revision and not self.config['monitor'].get('paused'):
                    for name in job.due:
                        self.source_due[name] = self.clock() + self.config['sources'][name].get('interval_seconds', self.config['monitor']['interval_seconds'])
                    self.next_scan = min(self.source_due.values(), default=float('inf'))
                    if error:
                        self.store.set_state('last_scan_error', error)
                        with self.store.cycle_lock():
                            for name in job.due:
                                self.store.source_error(name, error)
                    else:
                        from .models import Vacancy
                        with self.store.cycle_lock():
                            for name, summary in result['sources'].items():
                                if summary.get('error'):
                                    self.store.source_error(name, summary['error'])
                                else:
                                    vacancies = [Vacancy(**item['vacancy']) for item in result['vacancies'] if item['vacancy']['source'] == name]
                                    self.store.save_snapshot(name, [(v, decide_profiles(v, self.config, self.config['sources'][name].get('notify_unverified', False))) for v in vacancies], rules=combined_collection_filters(self.config))
                            self.store.set_state('last_scan_error', '')
                        self.dispatch_pending()
        if self.config['monitor'].get('paused'):
            return
        if not self.background and self.clock() >= self.next_scan:
            self.request_check(force=False)
        elif self.clock() >= self.next_delivery:
            self.dispatch_pending()

    def close(self):
        if self.background:
            self.background.close()

    def check_and_dispatch(self, force=True):
        # Schedule before work, and again after completion. Delivery failures must
        # not cause the full source scan to restart every minute.
        self.next_scan = self.clock() + self.config['monitor']['interval_seconds']
        self.next_delivery = self.clock() + 60
        due = [name for name in self.enabled if force or self.source_due.get(name, 0) <= self.clock()]
        selected = deepcopy(self.config)
        for name, source in selected['sources'].items():
            source['enabled'] = name in due
        try:
            with self.store.cycle_lock():
                result = scan(selected, self.store) if due else {'sources': {}, 'vacancies': []}
                sent = self._dispatch()
                log.info('Проверка завершена: %s; отправлено: %s', result['sources'], sent)
                return int(any(row.get('error') for row in result['sources'].values()))
        finally:
            for name in due:
                self.source_due[name] = self.clock() + self.config['sources'][name].get('interval_seconds', self.config['monitor']['interval_seconds'])
            self.next_scan = min((self.source_due[name] for name in self.enabled), default=float('inf'))
            self.next_delivery = self.clock() + 60

    def dispatch_pending(self):
        self.next_delivery = self.clock() + 60
        try:
            with self.store.cycle_lock():
                return self._dispatch()
        finally:
            self.next_delivery = self.clock() + 60

    def run_due(self):
        if self.config['monitor'].get('paused'):
            return None
        if self.clock() >= self.next_scan:
            if not allows_scan(self.config['monitor'].get('schedule', 'off')):
                self.next_scan = self.clock() + 60
                if self.clock() >= self.next_delivery:
                    return self.dispatch_pending()
                return None
            return self.check_and_dispatch(force=False)
        if self.clock() >= self.next_delivery:
            return self.dispatch_pending()
        return None
