"""Keep the original SQLite sessions and pending messages usable after UI migration."""
import json
import sqlite3

from agentbot.store import Store


def create_legacy(path):
    db = sqlite3.connect(path)
    db.executescript('''
        CREATE TABLE sessions (id TEXT PRIMARY KEY, created REAL NOT NULL);
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE runs (
            update_id INTEGER PRIMARY KEY, session TEXT NOT NULL, message TEXT UNIQUE NOT NULL,
            prompt TEXT, state TEXT NOT NULL, created REAL NOT NULL,
            answer TEXT NOT NULL DEFAULT '', sent INTEGER NOT NULL DEFAULT 0,
            notified INTEGER NOT NULL DEFAULT 0, stop_requested INTEGER NOT NULL DEFAULT 0);
        INSERT INTO sessions VALUES ('ses_original_first',100);
        INSERT INTO sessions VALUES ('ses_original_second',200);
        INSERT INTO meta VALUES ('current','ses_original_second');
        INSERT INTO runs VALUES (7,'ses_original_first','msg_original',NULL,'done',100,'prior answer',4,0,0);
        INSERT INTO runs VALUES (8,'ses_original_second','msg_uncertain','pending task','submitting',200,'',0,0,1);
    ''')
    db.commit()
    db.close()


def test_legacy_sessions_get_stable_short_numbers_without_losing_runs(tmp_path):
    path = tmp_path / 'legacy.sqlite3'
    create_legacy(path)
    store = Store(path)
    try:
        assert store.current() == 'ses_original_second'
        assert store.numbered(1)['id'] == 'ses_original_first'
        assert store.numbered(2)['id'] == 'ses_original_second'
        assert store.get(7)['answer'] == 'prior answer'
        assert store.get(7)['sent'] == 4 and store.get(7)['notified'] == 0
        assert store.get(8)['state'] == 'unknown'
        assert store.get(8)['prompt'] == 'pending task'
        assert store.get(8)['stop_requested'] == 1
        store.edit_session('ses_original_first', title='Renamed old task')
        store.add_session('ses_new', title='New task')
        assert store.numbered(3)['id'] == 'ses_new'
    finally:
        store.close()
    for _ in range(2):
        store = Store(path)
        try:
            assert [(store.session(sid)['number'], store.session(sid)['title']) for sid in
                    ['ses_original_first', 'ses_original_second', 'ses_new']] == [
                        (1, 'Renamed old task'), (2, 'Без названия'), (3, 'New task')]
            assert store.get(8)['message'] == 'msg_uncertain'
        finally:
            store.close()


def test_draft_and_uploaded_file_metadata_survive_migration_restart(tmp_path):
    path = tmp_path / 'legacy.sqlite3'
    create_legacy(path)
    store = Store(path)
    store.add_session('ses_draft', title='Draft task', kind='task')
    payload = {'title': 'Draft task', 'description': 'Only the assigned files', 'kind': 'task'}
    store.set_wizard('ses_draft', 'files', payload)
    file = {'upload_id': 'fixture-upload', 'name': 'original.bin', 'path': '/private/task/files/original.bin', 'size': 7}
    store.add_file('ses_draft', file)
    store.close()
    store = Store(path)
    try:
        assert store.wizard() == {'sid': 'ses_draft', 'stage': 'files', 'payload': payload}
        assert json.loads(store.session('ses_draft')['files']) == [file]
        store.set_wizard()
        store.resume_wizard('ses_draft')
        assert store.wizard()['payload'] == payload
        store.add_file('ses_draft', file)
        assert json.loads(store.session('ses_draft')['files']) == [file]
        store.prepare(9, 'ses_draft', 'private workspace reference only', clear_draft=True)
        assert store.wizard() is None
        assert store.session('ses_draft')['stage'] == 'active'
    finally:
        store.close()
