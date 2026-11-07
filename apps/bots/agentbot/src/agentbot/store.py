import os
import sqlite3
import time
import uuid
import json
import unicodedata
from pathlib import Path


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path)
        os.chmod(path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS wizard_receipts (update_id INTEGER PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS runs (
                update_id INTEGER PRIMARY KEY, session TEXT NOT NULL, message TEXT UNIQUE NOT NULL,
                prompt TEXT, state TEXT NOT NULL, created REAL NOT NULL,
                answer TEXT NOT NULL DEFAULT '', sent INTEGER NOT NULL DEFAULT 0,
                notified INTEGER NOT NULL DEFAULT 0, stop_requested INTEGER NOT NULL DEFAULT 0);
        """)
        columns = {r[1] for r in self.db.execute('PRAGMA table_info(sessions)')}
        for name, sql in [('number', 'INTEGER'), ('title', "TEXT NOT NULL DEFAULT 'Без названия'"),
                          ('kind', "TEXT NOT NULL DEFAULT 'task'"), ('stage', "TEXT NOT NULL DEFAULT 'active'"),
                          ('details', "TEXT NOT NULL DEFAULT '{}'"), ('files', "TEXT NOT NULL DEFAULT '[]'")]:
            if name not in columns:
                self.db.execute(f'ALTER TABLE sessions ADD COLUMN {name} {sql}')
        with self.db:
            self.db.execute('UPDATE sessions SET number=rowid WHERE number IS NULL')
            self.db.execute('CREATE UNIQUE INDEX IF NOT EXISTS sessions_number ON sessions(number)')
        # A crash during POST has an ambiguous outcome. Never automatically replay it.
        with self.db:
            self.db.execute("UPDATE runs SET state='unknown' WHERE state='submitting'")

    def close(self):
        self.db.close()

    def current(self):
        row = self.db.execute("SELECT value FROM meta WHERE key='current'").fetchone()
        return row[0] if row else None

    def select(self, sid):
        if not self.db.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone():
            raise ValueError("Unknown session")
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES ('current', ?)", (sid,))

    def add_session(self, sid, title='Без названия', kind='task'):
        with self.db:
            number = self.db.execute('SELECT COALESCE(MAX(number),0)+1 FROM sessions').fetchone()[0]
            self.db.execute("INSERT INTO sessions (id,created,number,title,kind) VALUES (?,?,?,?,?)", (sid, time.time(), number, title[:120], kind))
        self.select(sid)

    def session(self, sid):
        return self.db.execute('SELECT * FROM sessions WHERE id=?', (sid,)).fetchone()

    def numbered(self, number):
        return self.db.execute('SELECT * FROM sessions WHERE number=?', (number,)).fetchone()

    def label(self, sid):
        row=self.session(sid)
        if not row:return 'Задача'
        title=''.join(c for c in ' '.join(row['title'].split()) if unicodedata.category(c) not in {'Cc','Cf'})
        return f'#{row["number"]} · {title[:60]}'

    def edit_session(self, sid, **fields):
        allowed = {'title', 'kind', 'stage', 'details', 'files'}
        if not fields or not fields.keys() <= allowed:
            raise ValueError('Invalid session fields')
        with self.db:
            self.db.execute('UPDATE sessions SET '+','.join(f'{key}=?' for key in fields)+' WHERE id=?', (*fields.values(),sid))

    def wizard(self):
        row = self.db.execute("SELECT value FROM meta WHERE key='wizard'").fetchone()
        return json.loads(row[0]) if row else None

    def wizard_handled(self, uid):
        return self.db.execute('SELECT 1 FROM wizard_receipts WHERE update_id=?',(uid,)).fetchone() is not None

    def set_wizard(self, sid=None, stage=None, payload=None, *, update_id=None):
        with self.db:
            if sid is None:
                self.db.execute("DELETE FROM meta WHERE key='wizard'")
            else:
                value = {'sid':sid, 'stage':stage, 'payload':payload or {}}
                self.db.execute("INSERT OR REPLACE INTO meta VALUES ('wizard',?)", (json.dumps(value),))
                if stage != 'rename':
                    self.db.execute('UPDATE sessions SET stage=?,details=? WHERE id=?', (stage,json.dumps(payload or {}),sid))
            if update_id is not None:
                self.db.execute('INSERT OR IGNORE INTO wizard_receipts VALUES (?)',(update_id,))

    def resume_wizard(self, sid):
        row = self.session(sid)
        if row and row['stage'] != 'active':
            self.set_wizard(sid,row['stage'],json.loads(row['details']))

    def add_file(self, sid, file):
        files=json.loads(self.session(sid)['files'])
        if not any(f.get('upload_id')==file.get('upload_id') for f in files):
            files.append(file)
        self.edit_session(sid,files=json.dumps(files))

    def sessions(self):
        return self.db.execute("SELECT * FROM sessions ORDER BY created DESC").fetchall()

    def latest(self, sid):
        return self.db.execute("SELECT * FROM runs WHERE session=? ORDER BY created DESC LIMIT 1", (sid,)).fetchone()

    def get(self, uid):
        return self.db.execute("SELECT * FROM runs WHERE update_id=?", (uid,)).fetchone()

    def prepare(self, uid, sid, prompt, *, clear_draft=False):
        if self.get(uid):
            return False
        latest = self.latest(sid)
        if latest and latest['state'] not in {'done', 'error', 'stopped'}:
            raise ValueError("Session busy")
        # OpenCode IDs use a msg_ prefix; timestamp also keeps IDs ordered.
        mid = f"msg_{int(time.time() * 1000):012x}{uuid.uuid4().hex[:14]}"
        with self.db:
            self.db.execute("INSERT INTO runs (update_id,session,message,prompt,state,created) VALUES (?,?,?,?,'prepared',?)",
                            (uid, sid, mid, prompt, time.time()))
            if clear_draft:
                self.db.execute("DELETE FROM meta WHERE key='wizard'")
                self.db.execute("UPDATE sessions SET stage='active' WHERE id=?", (sid,))
        return True

    def update(self, uid, **fields):
        allowed = {'state', 'prompt', 'answer', 'sent', 'notified', 'stop_requested'}
        if not fields or not fields.keys() <= allowed:
            raise ValueError("Invalid fields")
        with self.db:
            self.db.execute("UPDATE runs SET " + ','.join(f'{key}=?' for key in fields) + " WHERE update_id=?",
                            (*fields.values(), uid))

    def pending(self):
        return self.db.execute("SELECT * FROM runs WHERE state NOT IN ('done','error','stopped') OR notified=0 ORDER BY created").fetchall()
