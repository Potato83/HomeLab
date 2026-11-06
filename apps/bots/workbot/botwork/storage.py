from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from .locking import database_path, file_lock


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS vacancies (
            source TEXT NOT NULL, external_id TEXT NOT NULL, payload TEXT NOT NULL,
            matched INTEGER NOT NULL, active INTEGER NOT NULL,
            generation INTEGER NOT NULL DEFAULT 1,
            sent_generation INTEGER NOT NULL DEFAULT 0,
            first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
            PRIMARY KEY (source, external_id)
        );
        CREATE TABLE IF NOT EXISTS source_status (
            source TEXT PRIMARY KEY, last_attempt TEXT, last_success TEXT,
            error TEXT, found INTEGER, matched INTEGER
        );
        CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS site_overrides (
            id TEXT PRIMARY KEY, item TEXT, hidden INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS site_commands (
            update_id INTEGER PRIMARY KEY, response TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS settings_commands (update_id INTEGER PRIMARY KEY, response TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS site_imports (token TEXT PRIMARY KEY, payload TEXT NOT NULL, expires INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS source_health (
            source TEXT PRIMARY KEY,
            failure_count INTEGER NOT NULL DEFAULT 0,
            failure_since TEXT,
            incident INTEGER NOT NULL DEFAULT 0,
            alerted_incident INTEGER NOT NULL DEFAULT 0,
            recovered_incident INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS tracked_vacancies (
            ref TEXT PRIMARY KEY, source TEXT NOT NULL, external_id TEXT NOT NULL,
            status TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS profile_commands (update_id INTEGER PRIMARY KEY, response TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS feedback_commands (update_id INTEGER PRIMARY KEY, response TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS career_details (
            ref TEXT PRIMARY KEY, note TEXT NOT NULL DEFAULT '', contact TEXT NOT NULL DEFAULT '',
            next_action TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS reminders (
            ref TEXT PRIMARY KEY, due_at TEXT NOT NULL, text TEXT NOT NULL,
            delivered_at TEXT
        );
        CREATE TABLE IF NOT EXISTS career_commands (update_id INTEGER PRIMARY KEY, response TEXT NOT NULL);
        """)
        # Upgrade older databases without modifying vacancy delivery history.
        # A pre-upgrade error confirms one failure, not an unknown number of failures.
        with self.db:
            self.db.execute("""INSERT OR IGNORE INTO source_health
                (source, failure_count, failure_since, incident)
                SELECT source, CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END,
                    CASE WHEN error IS NOT NULL THEN last_attempt ELSE NULL END,
                    CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END
                FROM source_status""")

    def close(self):
        self.db.close()

    def refilter(self, config, events_only=False):
        from .models import Vacancy
        from .profile_matching import combined_collection_filters, decide_profiles
        with self.db:
            for row in self.db.execute('SELECT * FROM vacancies').fetchall():
                payload = json.loads(row['payload'])
                v = Vacancy(**payload['vacancy'])
                if events_only and v.kind != 'event':
                    continue
                source = config['sources'].get(v.source, {})
                decision = decide_profiles(v, config, source.get('notify_unverified', False))
                matching_profiles = decision.matching_profiles
                if v.kind == 'event' and payload.get('_notification_kind') in ('event_cancelled', 'event_restored') and row['generation'] > row['sent_generation']:
                    from .event_state import change_decision
                    decision = change_decision(v, payload, combined_collection_filters(config))
                elif v.kind == 'event' and payload.get('_notification_kind') == 'event_update' and row['sent_generation'] > 0 and row['generation'] > row['sent_generation']:
                    from .event_state import update_decision
                    decision = update_decision(v, payload, combined_collection_filters(config))
                decision.matching_profiles = matching_profiles
                payload['decision'] = asdict(decision)
                matched = decision.matched and not (v.recruitment_status == 'unknown' and payload.get('_notified_status') and payload.get('_notification_kind') not in ('event_update', 'event_cancelled', 'event_restored'))
                self.db.execute('UPDATE vacancies SET matched=?,payload=? WHERE source=? AND external_id=?',
                    (matched, json.dumps(payload, ensure_ascii=False), v.source, v.external_id))
            self.db.execute('UPDATE source_status SET matched=(SELECT COUNT(*) FROM vacancies v WHERE v.source=source_status.source AND v.active=1 AND v.matched=1)')

    def watched_events(self, source):
        from datetime import timedelta
        from .events import instant, event_now
        from .models import Vacancy
        watched = []
        for row in self.db.execute('SELECT payload FROM vacancies WHERE source=? AND sent_generation>0', (source,)):
            payload = json.loads(row['payload'])
            v = Vacancy(**payload['vacancy'])
            if v.kind != 'event':
                continue
            try:
                until = instant(payload.get('_event_watch_until') or v.end_at)
            except ValueError:
                continue
            if until >= event_now() - timedelta(days=1):
                watched.append(v.to_dict())
        return watched

    def cycle_lock(self):
        path = database_path(self.db.execute("PRAGMA database_list").fetchone()[2])
        return file_lock(str(path) + '.lock', 'Другой процесс уже выполняет проверку')

    def save_snapshot(self, source, records, rules=None):
        """Commit only complete source snapshots; failed sources retain previous state."""
        timestamp = now()
        ids = {v.external_id for v, _ in records}
        with self.db:
            for v, decision in records:
                old = self.db.execute("SELECT * FROM vacancies WHERE source=? AND external_id=?", (source, v.external_id)).fetchone()
                generation = old["generation"] if old else 1
                previous = json.loads(old['payload']) if old else {}
                notified = previous.get('_notified_status', '')
                if old and '_notified_status' not in previous and old['sent_generation'] >= old['generation']:
                    notified = previous['vacancy'].get('recruitment_status', 'open')
                kind = previous.get('_notification_kind', '')
                event_meta = {}
                if v.kind == 'event':
                    from .event_state import transition
                    generation, notified, kind, event_meta, decision = transition(v, previous, old, decision,
                        rules if rules is not None else {'events_enabled': True})
                elif old and v.active and not old["active"]:
                    generation += 1
                    notified, kind = '', ''
                elif old and v.active and v.recruitment_status == 'open' and notified == 'unknown' and old['generation'] == old['sent_generation']:
                    generation += 1
                    kind = 'confirmation'
                # A pending confirmation must wait for confirmation, not be resent
                # as another unknown-status candidate after a temporary form outage.
                queue_match = decision.matched and not (v.recruitment_status == 'unknown' and notified and kind not in ('event_update', 'event_cancelled', 'event_restored'))
                payload = json.dumps({"vacancy": asdict(v), "decision": asdict(decision),
                    '_notified_status': notified, '_notification_kind': kind, **event_meta}, ensure_ascii=False)
                active = v.active or (v.kind == 'event' and kind == 'event_cancelled' and decision.matched)
                self.db.execute("""INSERT INTO vacancies
                    (source, external_id, payload, matched, active, generation, sent_generation, first_seen, last_seen)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source, external_id) DO UPDATE SET
                    payload=excluded.payload, matched=excluded.matched, active=excluded.active,
                    generation=excluded.generation, last_seen=excluded.last_seen""",
                    (source, v.external_id, payload, queue_match, active, generation,
                     old["sent_generation"] if old else 0, old["first_seen"] if old else timestamp, timestamp))
            for old in self.db.execute("SELECT external_id FROM vacancies WHERE source=? AND active=1", (source,)).fetchall():
                if old["external_id"] not in ids:
                    self.db.execute("UPDATE vacancies SET active=0, matched=0 WHERE source=? AND external_id=?", (source, old["external_id"]))
            self.db.execute("""INSERT INTO source_status(source, last_attempt, last_success, error, found, matched)
                VALUES (?, ?, ?, NULL, ?, ?) ON CONFLICT(source) DO UPDATE SET
                last_attempt=excluded.last_attempt, last_success=excluded.last_success,
                error=NULL, found=excluded.found, matched=excluded.matched""",
                (source, timestamp, timestamp, len(records), sum(d.matched for _, d in records)))
            self.db.execute("""INSERT INTO source_health(source) VALUES (?)
                ON CONFLICT(source) DO UPDATE SET failure_count=0, failure_since=NULL""", (source,))
            self.db.execute("INSERT INTO state VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ('unverified:' + source, str(sum(v.active and v.recruitment_status == 'unknown' for v, _ in records))))
            self.db.execute("INSERT INTO state VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ('event_watch_errors:' + source, str(sum(bool(v.event_check_error) for v, _ in records))))

    def source_error(self, source, error):
        timestamp = now()
        with self.db:
            self.db.execute("""INSERT INTO source_status(source, last_attempt, error) VALUES (?, ?, ?)
                ON CONFLICT(source) DO UPDATE SET last_attempt=excluded.last_attempt, error=excluded.error""",
                (source, timestamp, error))
            self.db.execute("""INSERT INTO source_health(source, failure_count, failure_since, incident)
                VALUES (?, 1, ?, 1) ON CONFLICT(source) DO UPDATE SET
                failure_since=CASE WHEN failure_count=0 THEN excluded.failure_since ELSE failure_since END,
                incident=CASE WHEN failure_count=0 THEN incident+1 ELSE incident END,
                failure_count=failure_count+1""", (source, timestamp))

    def pending(self, enabled_sources, limit):
        if not enabled_sources:
            return []
        placeholders = ",".join("?" for _ in enabled_sources)
        return self.db.execute(f"""SELECT * FROM vacancies WHERE active=1 AND matched=1
            AND sent_generation < generation AND source IN ({placeholders})
            AND NOT EXISTS (SELECT 1 FROM tracked_vacancies t WHERE t.source=vacancies.source AND t.external_id=vacancies.external_id AND t.status='ignored')
            ORDER BY first_seen, source, external_id LIMIT ?""", (*enabled_sources, limit)).fetchall()

    def sent_vacancies(self):
        """Return active, delivered original listings for cross-source deduplication."""
        result = []
        rows = self.db.execute("""SELECT source, external_id, payload FROM vacancies
            WHERE active=1 AND sent_generation >= generation""")
        for row in rows:
            payload = json.loads(row['payload'])
            vacancy = payload.get('vacancy', {})
            kind = payload.get('_notification_kind', '')
            if vacancy.get('kind', 'vacancy') == 'vacancy' or (
                vacancy.get('kind') == 'event' and kind in ('', 'confirmation')
                and not payload.get('_event_cancel_notified', False)
            ):
                result.append((row['source'], row['external_id'], vacancy))
        return result

    def set_tracking(self, ref, status):
        with self.db:
            return self._set_tracking(ref, status)

    def _set_tracking(self, ref, status):
        from .pipeline import STATUSES
        vacancy = self.vacancy_by_ref(ref)
        if vacancy is None:
            return None
        source, external_id = vacancy['source'], vacancy['external_id']
        if status == 'remove':
            self.db.execute('DELETE FROM tracked_vacancies WHERE ref=?', (ref,))
        else:
            if status not in STATUSES:
                raise ValueError('Неизвестный статус отклика')
            self.db.execute('''INSERT INTO tracked_vacancies VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(ref) DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at''',
                (ref, source, external_id, status, now()))
        return {'ref': ref, 'title': vacancy.get('title', ''), 'url': vacancy.get('url', '')}

    def vacancy_by_ref(self, ref):
        from .pipeline import vacancy_ref
        import re
        if not isinstance(ref, str) or not re.fullmatch('[0-9a-f]{8}', ref):
            raise ValueError('ID вакансии должен содержать 8 символов из карточки.')
        matches = []
        for row in self.db.execute('SELECT source, external_id, payload FROM vacancies'):
            if vacancy_ref(row['source'], row['external_id']) == ref:
                vacancy = json.loads(row['payload'])['vacancy']
                if vacancy.get('kind', 'vacancy') == 'vacancy':
                    matches.append(vacancy)
        if len(matches) > 1:
            raise ValueError('ID совпал у нескольких карточек. Изменение отменено.')
        return matches[0] if matches else None

    def apply_feedback(self, update_id, ref, status):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            old = self.db.execute('SELECT response FROM feedback_commands WHERE update_id=?', (update_id,)).fetchone()
            if old:
                return old['response']
            vacancy = self._set_tracking(ref, status)
            response = ('Сохранено в /track' if status == 'saved' else 'Скрыто. Вернуть можно через /track ignored') if vacancy else 'Вакансия не найдена'
            self.db.execute('INSERT INTO feedback_commands VALUES (?, ?)', (update_id, response))
            return response

    def tracked_vacancies(self, ignored=False):
        rows = self.db.execute('''SELECT t.ref, t.status, v.payload FROM tracked_vacancies t
            LEFT JOIN vacancies v ON v.source=t.source AND v.external_id=t.external_id
            WHERE (t.status='ignored')=? ORDER BY t.updated_at DESC''', (int(ignored),))
        result = []
        for row in rows:
            payload = json.loads(row['payload']) if row['payload'] else {}
            vacancy = payload.get('vacancy', {})
            result.append({'ref': row['ref'], 'status': row['status'],
                'title': vacancy.get('title', 'Объявление больше недоступно'),
                'company': vacancy.get('company', ''), 'url': vacancy.get('url', '')})
        return result

    def pending_event_cancellations(self, enabled_sources, limit):
        """Select confirmed cancellation notices ahead of a scheduled digest."""
        event_sources = [name for name in enabled_sources if name in ('ctftime', 'ods_events')]
        if not event_sources or limit <= 0:
            return []
        placeholders = ",".join("?" for _ in event_sources)
        cursor = self.db.execute(f"""SELECT * FROM vacancies WHERE active=1 AND matched=1
            AND sent_generation < generation AND source IN ({placeholders})
            ORDER BY first_seen, source, external_id""", event_sources)
        cancellations = []
        for row in cursor:
            if json.loads(row['payload']).get('_notification_kind') == 'event_cancelled':
                cancellations.append(row)
                if len(cancellations) == limit:
                    break
        return cancellations

    def mark_sent(self, row):
        payload = json.loads(row['payload'])
        payload['_notified_status'] = payload['vacancy'].get('recruitment_status', 'open')
        if payload['vacancy'].get('kind') == 'event':
            cancelled = payload.get('_notification_kind') == 'event_cancelled'
            payload['_event_cancel_notified'] = cancelled
            payload.pop('_event_changed_fields', None)
            if not cancelled:
                payload['_event_watch_until'] = payload['vacancy']['end_at']
        with self.db:
            self.db.execute("UPDATE vacancies SET sent_generation=?, payload=? WHERE source=? AND external_id=?",
                (row["generation"], json.dumps(payload, ensure_ascii=False), row["source"], row["external_id"]))

    def get_state(self, key, default=None):
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_state(self, key, value):
        with self.db:
            self.db.execute("INSERT INTO state VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    def status(self):
        return [dict(row) for row in self.db.execute("""SELECT s.*,
            COALESCE(h.failure_count, 0) AS failure_count, h.failure_since,
            COALESCE(h.incident, 0) AS incident,
            COALESCE(h.alerted_incident, 0) AS alerted_incident,
            COALESCE(h.recovered_incident, 0) AS recovered_incident
            FROM source_status s LEFT JOIN source_health h ON h.source=s.source
            ORDER BY s.source""")]

    def pending_counts(self, enabled_sources):
        if not enabled_sources:
            return {}
        placeholders = ",".join("?" for _ in enabled_sources)
        return {row["source"]: row["count"] for row in self.db.execute(f"""
            SELECT source, COUNT(*) AS count FROM vacancies
            WHERE active=1 AND matched=1 AND sent_generation < generation
            AND NOT EXISTS (SELECT 1 FROM tracked_vacancies t WHERE t.source=vacancies.source AND t.external_id=vacancies.external_id AND t.status='ignored')
            AND source IN ({placeholders}) GROUP BY source""", enabled_sources)}

    def acknowledge_health(self, source, kind, incident):
        column = {"failure": "alerted_incident", "recovery": "recovered_incident"}[kind]
        with self.db:
            self.db.execute(f"UPDATE source_health SET {column}=MAX({column}, ?) WHERE source=?",
                            (incident, source))
