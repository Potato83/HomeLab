"""Small SQLite queue, compatible with the previous delivery history."""
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
from .locking import database_path, file_lock
from .matching import decide
from .models import Vacancy

def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')

class Store:
    def __init__(self, path):
        path = database_path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        path.chmod(0o600)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS vacancies (
            source TEXT NOT NULL, external_id TEXT NOT NULL, payload TEXT NOT NULL,
            matched INTEGER NOT NULL, active INTEGER NOT NULL,
            generation INTEGER NOT NULL DEFAULT 1,
            sent_generation INTEGER NOT NULL DEFAULT 0,
            first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
            PRIMARY KEY(source, external_id));
        CREATE TABLE IF NOT EXISTS source_status (
            source TEXT PRIMARY KEY, last_attempt TEXT, last_success TEXT,
            error TEXT, found INTEGER, matched INTEGER);
        CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        # Legacy career notes/tables are left intact; the application no longer reads them.

    def close(self):
        self.db.close()

    def cycle_lock(self):
        return file_lock(str(database_path(self.db.execute('PRAGMA database_list').fetchone()[2])) + '.lock',
                         'Другой процесс уже выполняет проверку')

    def save_snapshot(self, source, records, rules=None):
        stamp = now()
        ids = {v.external_id for v, _ in records}
        with self.db:
            for v, decision in records:
                payload = json.dumps({'vacancy': asdict(v), 'decision': asdict(decision)}, ensure_ascii=False)
                self.db.execute("""
                INSERT INTO vacancies(source,external_id,payload,matched,active,first_seen,last_seen)
                VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(source,external_id) DO UPDATE SET
                payload=excluded.payload,matched=excluded.matched,active=excluded.active,last_seen=excluded.last_seen
                """, (source, v.external_id, payload, decision.matched, v.active, stamp, stamp))
            for old in self.db.execute('SELECT external_id FROM vacancies WHERE source=? AND active=1', (source,)).fetchall():
                if old['external_id'] not in ids:
                    self.db.execute('UPDATE vacancies SET active=0,matched=0 WHERE source=? AND external_id=?', (source, old['external_id']))
            self.db.execute("""
            INSERT INTO source_status(source,last_attempt,last_success,error,found,matched) VALUES(?,?,?,NULL,?,?)
            ON CONFLICT(source) DO UPDATE SET last_attempt=excluded.last_attempt,last_success=excluded.last_success,
            error=NULL,found=excluded.found,matched=excluded.matched
            """, (source, stamp, stamp, len(records), sum(d.matched for _, d in records)))

    def source_error(self, source, error):
        with self.db:
            self.db.execute("""
            INSERT INTO source_status(source,last_attempt,error) VALUES(?,?,?)
            ON CONFLICT(source) DO UPDATE SET last_attempt=excluded.last_attempt,error=excluded.error
            """, (source, now(), error[:300]))

    def pending(self, enabled_sources, limit):
        rows = self.db.execute('''SELECT v.* FROM vacancies v LEFT JOIN source_status s USING(source)
            WHERE v.active=1 AND v.matched=1 AND v.sent_generation<v.generation AND s.error IS NULL
            ORDER BY first_seen DESC,source,external_id''')
        return [r for r in rows if r['source'] in enabled_sources][:limit]

    def mark_sent(self, row):
        with self.db:
            self.db.execute('UPDATE vacancies SET sent_generation=? WHERE source=? AND external_id=?',
                            (row['generation'], row['source'], row['external_id']))

    def suppress(self, row):
        with self.db:
            self.db.execute('UPDATE vacancies SET matched=0 WHERE source=? AND external_id=?',
                            (row['source'], row['external_id']))

    def sent_vacancies(self):
        return [(r['source'], r['external_id'], json.loads(r['payload'])['vacancy'])
                for r in self.db.execute('SELECT * FROM vacancies WHERE sent_generation>0')]

    def refilter(self, rules):
        with self.db:
            for row in self.db.execute('SELECT * FROM vacancies').fetchall():
                payload = json.loads(row['payload'])
                d = decide(Vacancy(**payload['vacancy']), rules)
                payload['decision'] = asdict(d)
                self.db.execute('UPDATE vacancies SET matched=?,payload=? WHERE source=? AND external_id=?',
                                (d.matched, json.dumps(payload, ensure_ascii=False), row['source'], row['external_id']))

    def search(self, kind, rules, enabled_sources, query='', limit=5):
        result = []
        selected = dict(rules)
        if query:
            selected['include_words'] = [query[:60]]
        order = "julianday(json_extract(payload,'$.vacancy.start_at')),first_seen DESC" if kind == 'event' else 'first_seen DESC'
        for row in self.db.execute('SELECT * FROM vacancies WHERE active=1 ORDER BY ' + order + ',source,external_id'):
            payload = json.loads(row['payload'])
            v = Vacancy(**payload['vacancy'])
            if row['source'] in enabled_sources and v.kind == kind:
                d = decide(v, selected)
                if d.matched:
                    payload['decision'] = asdict(d)
                    result.append(payload)
                    if len(result) == limit:
                        break
        return result

    def get_state(self, key, default=None):
        row = self.db.execute('SELECT value FROM state WHERE key=?', (key,)).fetchone()
        return row[0] if row else default

    def set_state(self, key, value):
        with self.db:
            self.db.execute('INSERT INTO state VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, str(value)))

    def status(self):
        return [dict(r) for r in self.db.execute('SELECT * FROM source_status ORDER BY source')]
