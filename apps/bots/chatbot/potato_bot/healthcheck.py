from __future__ import annotations

import sqlite3
import sys

from . import config
from .database import SCHEMA_VERSION

REQUIRED_TABLES = {
    "users",
    "potatoes",
    "settings",
    "chats",
    "inventory",
    "winners",
    "operations",
    "games",
    "admin_confirmations",
    "admin_audit",
    "loans",
    "loan_notifications",
    "sleep_actions",
}


def main() -> int:
    try:
        database_uri = f"{config.DATABASE_PATH.as_uri()}?mode=ro"
        with sqlite3.connect(database_uri, uri=True, timeout=5) as connection:
            connection.row_factory = sqlite3.Row
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            tables = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        ready = version == SCHEMA_VERSION and REQUIRED_TABLES <= tables
        if not ready:
            print(
                "Database schema is not initialized or incomplete",
                file=sys.stderr,
            )
            return 1
        return 0
    except Exception as exc:
        print(f"Healthcheck failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
