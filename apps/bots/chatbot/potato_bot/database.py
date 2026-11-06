from __future__ import annotations

import fcntl
import os
import sqlite3
import time
from pathlib import Path

from . import config

DEFAULT_DB_PATH = config.DATABASE_PATH
SCHEMA_VERSION = 2


def acquire_database_lock(db_path: str | Path | None = None) -> int:
    path = Path(db_path or DEFAULT_DB_PATH).resolve()
    lock_path = Path(f"{path}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(descriptor)
        raise RuntimeError(f"database is already in use: {path}") from exc
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    path = Path(db_path or DEFAULT_DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=5)
    try:
        for protected_path in (
            path,
            Path(f"{path}-wal"),
            Path(f"{path}-shm"),
        ):
            try:
                os.chmod(protected_path, 0o600)
            except FileNotFoundError:
                pass
    except OSError:
        connection.close()
        raise
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _execute_script(connection: sqlite3.Connection, script: str) -> None:
    statement_lines: list[str] = []
    for line in script.splitlines():
        statement_lines.append(line)
        statement = "\n".join(statement_lines)
        if sqlite3.complete_statement(statement):
            if statement.strip():
                connection.execute(statement)
            statement_lines.clear()
    if any(line.strip() for line in statement_lines):
        raise ValueError("incomplete SQL statement in schema script")


def _create_core_schema(connection: sqlite3.Connection) -> None:
    _execute_script(
        connection,
        """
        CREATE TABLE IF NOT EXISTS users (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            full_name TEXT NOT NULL DEFAULT '',
            prefix TEXT,
            updated_at INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (chat_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS potatoes (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            amount INTEGER NOT NULL DEFAULT 0,
            last_dig_date INTEGER,
            wins INTEGER NOT NULL DEFAULT 0,
            games INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (chat_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS settings (
            chat_id INTEGER PRIMARY KEY,
            sleep_price INTEGER NOT NULL DEFAULT 10,
            dig_cd INTEGER NOT NULL DEFAULT 24,
            pvp_confirm INTEGER NOT NULL DEFAULT 1,
            sleep_duration INTEGER NOT NULL DEFAULT 2
        );

        CREATE TABLE IF NOT EXISTS winners (
            chat_id INTEGER PRIMARY KEY,
            winner_name TEXT NOT NULL,
            last_date TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS chats (
            chat_id INTEGER PRIMARY KEY,
            title TEXT
        );

        CREATE TABLE IF NOT EXISTS inventory (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            item_type TEXT NOT NULL,
            amount INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (chat_id, user_id, item_type)
        );

        CREATE TABLE IF NOT EXISTS loans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            request_key TEXT,
            principal INTEGER NOT NULL CHECK (principal > 0),
            due_amount INTEGER NOT NULL CHECK (due_amount >= principal),
            issued_at INTEGER NOT NULL,
            due_at INTEGER NOT NULL,
            next_loan_at INTEGER NOT NULL,
            settled_at INTEGER,
            status TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'settled'))
        );
        """
    )

def _create_transaction_schema(connection: sqlite3.Connection) -> None:
    _execute_script(
        connection,
        """
        CREATE TABLE IF NOT EXISTS operations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            operation_key TEXT NOT NULL UNIQUE,
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            delta INTEGER NOT NULL,
            balance_after INTEGER NOT NULL,
            kind TEXT NOT NULL,
            reference_id TEXT,
            created_at INTEGER NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_operations_account
            ON operations(chat_id, user_id, id);
        CREATE INDEX IF NOT EXISTS idx_operations_reference
            ON operations(reference_id);

        CREATE TABLE IF NOT EXISTS games (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL CHECK (
                kind IN ('blackjack', 'pvp', 'rps', 'flip', 'roulette')
            ),
            chat_id INTEGER NOT NULL,
            challenger_id INTEGER NOT NULL,
            target_id INTEGER,
            bet INTEGER NOT NULL CHECK (bet > 0),
            status TEXT NOT NULL CHECK (
                status IN (
                    'pending', 'active', 'settled', 'declined', 'expired'
                )
            ),
            challenger_reserved INTEGER NOT NULL DEFAULT 0
                CHECK (challenger_reserved IN (0, 1)),
            target_reserved INTEGER NOT NULL DEFAULT 0
                CHECK (target_reserved IN (0, 1)),
            payload TEXT NOT NULL DEFAULT '{}',
            message_id INTEGER,
            result TEXT,
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            settled_at INTEGER,
            version INTEGER NOT NULL DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_games_expiry
            ON games(status, expires_at);
        CREATE INDEX IF NOT EXISTS idx_games_open_chat
            ON games(chat_id, status);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_blackjack
            ON games(chat_id, challenger_id)
            WHERE kind = 'blackjack' AND status = 'active';

        CREATE TABLE IF NOT EXISTS admin_confirmations (
            token TEXT PRIMARY KEY,
            action TEXT NOT NULL,
            chat_id INTEGER NOT NULL,
            admin_id INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            used_at INTEGER
        );

        CREATE TABLE IF NOT EXISTS admin_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id INTEGER NOT NULL,
            chat_id INTEGER,
            action TEXT NOT NULL,
            details TEXT,
            created_at INTEGER NOT NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_loans_one_active
            ON loans(chat_id, user_id)
            WHERE status = 'active';
        CREATE UNIQUE INDEX IF NOT EXISTS idx_loans_request_key
            ON loans(request_key)
            WHERE request_key IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_loans_due
            ON loans(status, due_at);

        CREATE TABLE IF NOT EXISTS loan_notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            loan_id INTEGER NOT NULL UNIQUE,
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            due_amount INTEGER NOT NULL,
            balance INTEGER NOT NULL,
            user_name TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at INTEGER NOT NULL,
            sent_at INTEGER
        );

        CREATE INDEX IF NOT EXISTS idx_loan_notifications_pending
            ON loan_notifications(sent_at, next_attempt_at, id);

        CREATE TABLE IF NOT EXISTS sleep_actions (
            operation_key TEXT PRIMARY KEY,
            chat_id INTEGER NOT NULL,
            attacker_id INTEGER NOT NULL,
            target_id INTEGER NOT NULL,
            price INTEGER NOT NULL,
            duration_minutes INTEGER NOT NULL,
            shield_used INTEGER NOT NULL CHECK (shield_used IN (0, 1)),
            extended INTEGER NOT NULL CHECK (extended IN (0, 1)),
            until_date INTEGER NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN ('pending', 'applied', 'refunded', 'shield')
            )
        );

        CREATE INDEX IF NOT EXISTS idx_sleep_actions_pending_account
            ON sleep_actions(chat_id, attacker_id)
            WHERE status = 'pending';
        """
    )


def _create_integrity_triggers(connection: sqlite3.Connection) -> None:
    max_balance = int(config.MAX_BALANCE)
    connection.execute("DROP TRIGGER IF EXISTS potatoes_amount_insert_guard")
    connection.execute("DROP TRIGGER IF EXISTS potatoes_amount_update_guard")
    _execute_script(
        connection,
        f"""
        CREATE TRIGGER IF NOT EXISTS potatoes_amount_insert_guard
        BEFORE INSERT ON potatoes
        WHEN typeof(NEW.amount) != 'integer'
          OR NEW.amount < -{max_balance}
          OR NEW.amount > {max_balance}
        BEGIN
            SELECT RAISE(ABORT, 'invalid balance');
        END;

        CREATE TRIGGER IF NOT EXISTS potatoes_amount_update_guard
        BEFORE UPDATE OF amount ON potatoes
        WHEN typeof(NEW.amount) != 'integer'
          OR (NEW.amount < -{max_balance} AND NEW.amount <= OLD.amount)
          OR (NEW.amount > {max_balance} AND NEW.amount >= OLD.amount)
        BEGIN
            SELECT RAISE(ABORT, 'invalid balance');
        END;

        CREATE TRIGGER IF NOT EXISTS inventory_amount_insert_guard
        BEFORE INSERT ON inventory
        WHEN typeof(NEW.amount) != 'integer' OR NEW.amount < 0
        BEGIN
            SELECT RAISE(ABORT, 'invalid inventory amount');
        END;

        CREATE TRIGGER IF NOT EXISTS inventory_amount_update_guard
        BEFORE UPDATE OF amount ON inventory
        WHEN typeof(NEW.amount) != 'integer' OR NEW.amount < 0
        BEGIN
            SELECT RAISE(ABORT, 'invalid inventory amount');
        END;
        """
    )


def _migrate_v1_to_v2(connection: sqlite3.Connection) -> None:
    connection.execute("ALTER TABLE loans ADD COLUMN request_key TEXT")
    connection.execute(
        """
        CREATE UNIQUE INDEX idx_loans_request_key
        ON loans(request_key)
        WHERE request_key IS NOT NULL
        """
    )


def init_db(db_path: str | Path | None = None) -> None:
    path = Path(db_path or DEFAULT_DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)

    version = 0
    has_tables = False
    if path.exists() and path.stat().st_size > 0:
        with sqlite3.connect(path) as probe:
            version = probe.execute("PRAGMA user_version").fetchone()[0]
            has_tables = probe.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                LIMIT 1
                """
            ).fetchone()
        if has_tables and version not in {1, SCHEMA_VERSION}:
            raise RuntimeError(
                "unsupported database schema; start with an empty database"
            )

    with connect(path) as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.execute("BEGIN IMMEDIATE")
        if has_tables and version == 1:
            _migrate_v1_to_v2(connection)
        _create_core_schema(connection)
        _create_transaction_schema(connection)
        _create_integrity_triggers(connection)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def save_user_data(
    chat_id: int,
    user_id: int,
    full_name: str,
    chat_title: str | None,
    chat_type: str,
    db_path: str | Path | None = None,
) -> None:
    if user_id == 777000:
        return

    now = int(time.time())
    with connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO users (chat_id, user_id, full_name, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET
                full_name = excluded.full_name,
                updated_at = excluded.updated_at
            """,
            (chat_id, user_id, full_name, now),
        )
        if chat_type in {"group", "supergroup"}:
            connection.execute(
                """
                INSERT INTO chats (chat_id, title)
                VALUES (?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET title = excluded.title
                """,
                (chat_id, chat_title),
            )
