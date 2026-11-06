from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from ..database import connect

SETTING_VALUES = {
    "dig_cd": {1, 3, 6, 12, 24},
    "sleep_price": {5, 10, 20, 50, 100},
    "sleep_duration": {1, 2, 5, 10, 20, 30},
}
SETTING_UPDATE_SQL = {
    "dig_cd": "UPDATE settings SET dig_cd = ? WHERE chat_id = ?",
    "sleep_price": "UPDATE settings SET sleep_price = ? WHERE chat_id = ?",
    "sleep_duration": (
        "UPDATE settings SET sleep_duration = ? WHERE chat_id = ?"
    ),
}


class AdminOperationError(Exception):
    pass


class InvalidConfirmation(AdminOperationError):
    pass


@dataclass(frozen=True, slots=True)
class ResetResult:
    chat_id: int


@dataclass(frozen=True, slots=True)
class ChatSummary:
    chat_id: int
    title: str | None
    known_users: int
    total_balance: int
    total_debt: int
    active_loans: int
    active_games: int
    sleeping_users: int
    dig_cd: int
    sleep_price: int
    sleep_duration: int
    pvp_confirm: bool


@dataclass(frozen=True, slots=True)
class AdminAuditEntry:
    chat_id: int | None
    action: str
    details: str | None
    created_at: int


def get_chat_summary(
    chat_id: int,
    *,
    now: int | None = None,
    db_path: str | Path | None = None,
) -> ChatSummary | None:
    current_time = int(time.time()) if now is None else now
    with connect(db_path) as connection:
        connection.execute("BEGIN")
        row = connection.execute(
            """
            SELECT
                chats.title,
                (SELECT COUNT(*) FROM users WHERE chat_id = chats.chat_id)
                    AS known_users,
                (SELECT COUNT(*) FROM loans
                 WHERE chat_id = chats.chat_id AND status = 'active')
                    AS active_loans,
                (SELECT COUNT(*) FROM games
                 WHERE chat_id = chats.chat_id
                   AND status IN ('pending', 'active') AND expires_at > ?)
                    AS active_games,
                (SELECT COUNT(DISTINCT target_id) FROM sleep_actions
                 WHERE chat_id = chats.chat_id
                   AND status IN ('pending', 'applied') AND until_date > ?)
                    AS sleeping_users,
                COALESCE(settings.dig_cd, 24) AS dig_cd,
                COALESCE(settings.sleep_price, 10) AS sleep_price,
                COALESCE(settings.sleep_duration, 2) AS sleep_duration,
                COALESCE(settings.pvp_confirm, 1) AS pvp_confirm
            FROM chats
            LEFT JOIN settings ON settings.chat_id = chats.chat_id
            WHERE chats.chat_id = ?
            """,
            (current_time, current_time, chat_id),
        ).fetchone()
        balances = (
            connection.execute(
                "SELECT amount FROM potatoes WHERE chat_id = ?",
                (chat_id,),
            ).fetchall()
            if row is not None
            else []
        )
    if row is None:
        return None
    total_balance = sum(max(0, int(balance["amount"])) for balance in balances)
    total_debt = sum(max(0, -int(balance["amount"])) for balance in balances)
    return ChatSummary(
        chat_id=chat_id,
        title=row["title"],
        known_users=int(row["known_users"]),
        total_balance=total_balance,
        total_debt=total_debt,
        active_loans=int(row["active_loans"]),
        active_games=int(row["active_games"]),
        sleeping_users=int(row["sleeping_users"]),
        dig_cd=int(row["dig_cd"]),
        sleep_price=int(row["sleep_price"]),
        sleep_duration=int(row["sleep_duration"]),
        pvp_confirm=bool(row["pvp_confirm"]),
    )


def recent_admin_actions(
    *,
    limit: int = 15,
    db_path: str | Path | None = None,
) -> list[AdminAuditEntry]:
    with connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT chat_id, action, details, created_at
            FROM admin_audit
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [
        AdminAuditEntry(
            chat_id=(int(row["chat_id"]) if row["chat_id"] is not None else None),
            action=row["action"],
            details=row["details"],
            created_at=int(row["created_at"]),
        )
        for row in rows
    ]


def list_chats(
    *,
    db_path: str | Path | None = None,
) -> list[tuple[int, str | None]]:
    with connect(db_path) as connection:
        rows = connection.execute(
            "SELECT chat_id, title FROM chats ORDER BY title, chat_id"
        ).fetchall()
        return [(int(row["chat_id"]), row["title"]) for row in rows]


def toggle_pvp(
    chat_id: int,
    *,
    admin_id: int,
    db_path: str | Path | None = None,
) -> int:
    with connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT OR IGNORE INTO settings (chat_id) VALUES (?)",
            (chat_id,),
        )
        current = connection.execute(
            "SELECT pvp_confirm FROM settings WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
        new_value = 0 if int(current["pvp_confirm"]) == 1 else 1
        connection.execute(
            "UPDATE settings SET pvp_confirm = ? WHERE chat_id = ?",
            (new_value, chat_id),
        )
        connection.execute(
            """
            INSERT INTO admin_audit (
                admin_id, chat_id, action, details, created_at
            )
            VALUES (?, ?, 'toggle_pvp', ?, ?)
            """,
            (admin_id, chat_id, f"value={new_value}", int(time.time())),
        )
        return new_value


def get_pvp_setting(
    chat_id: int,
    *,
    db_path: str | Path | None = None,
) -> bool:
    with connect(db_path) as connection:
        row = connection.execute(
            "SELECT pvp_confirm FROM settings WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
        return bool(row["pvp_confirm"]) if row else True


def set_setting(
    chat_id: int,
    parameter: str,
    value: int,
    *,
    admin_id: int,
    db_path: str | Path | None = None,
) -> None:
    if parameter not in SETTING_VALUES or value not in SETTING_VALUES[parameter]:
        raise AdminOperationError("invalid setting value")

    with connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT OR IGNORE INTO settings (chat_id) VALUES (?)",
            (chat_id,),
        )
        connection.execute(
            SETTING_UPDATE_SQL[parameter],
            (value, chat_id),
        )
        connection.execute(
            """
            INSERT INTO admin_audit (
                admin_id, chat_id, action, details, created_at
            )
            VALUES (?, ?, 'set_setting', ?, ?)
            """,
            (
                admin_id,
                chat_id,
                f"{parameter}={value}",
                int(time.time()),
            ),
        )


def create_reset_confirmation(
    chat_id: int,
    admin_id: int,
    *,
    now: int | None = None,
    db_path: str | Path | None = None,
) -> str:
    current_time = int(time.time()) if now is None else now
    token = secrets.token_urlsafe(12)
    with connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO admin_confirmations (
                token, action, chat_id, admin_id, expires_at
            )
            VALUES (?, 'reset_chat', ?, ?, ?)
            """,
            (token, chat_id, admin_id, current_time + 120),
        )
        connection.execute(
            """
            DELETE FROM admin_confirmations
            WHERE expires_at < ? OR used_at IS NOT NULL
            """,
            (current_time,),
        )
    return token


def reset_chat(
    token: str,
    admin_id: int,
    *,
    now: int | None = None,
    db_path: str | Path | None = None,
) -> ResetResult:
    current_time = int(time.time()) if now is None else now
    with connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        confirmation = connection.execute(
            """
            SELECT chat_id
            FROM admin_confirmations
            WHERE token = ? AND action = 'reset_chat'
              AND admin_id = ? AND used_at IS NULL AND expires_at > ?
            """,
            (token, admin_id, current_time),
        ).fetchone()
        if confirmation is None:
            raise InvalidConfirmation("confirmation is invalid or expired")
        chat_id = int(confirmation["chat_id"])
        consumed = connection.execute(
            """
            UPDATE admin_confirmations
            SET used_at = ?
            WHERE token = ? AND admin_id = ?
              AND used_at IS NULL AND expires_at > ?
            """,
            (current_time, token, admin_id, current_time),
        )
        if consumed.rowcount != 1:
            raise InvalidConfirmation("confirmation was already used")
        connection.execute(
            """
            DELETE FROM admin_confirmations
            WHERE action = 'reset_chat' AND chat_id = ? AND used_at IS NULL
            """,
            (chat_id,),
        )

        connection.execute("DELETE FROM games WHERE chat_id = ?", (chat_id,))
        connection.execute(
            "DELETE FROM sleep_actions WHERE chat_id = ?", (chat_id,)
        )
        connection.execute(
            "DELETE FROM loan_notifications WHERE chat_id = ?", (chat_id,)
        )
        connection.execute("DELETE FROM loans WHERE chat_id = ?", (chat_id,))
        connection.execute(
            "DELETE FROM inventory WHERE chat_id = ?", (chat_id,)
        )
        connection.execute(
            "DELETE FROM operations WHERE chat_id = ?", (chat_id,)
        )
        connection.execute(
            "DELETE FROM potatoes WHERE chat_id = ?", (chat_id,)
        )
        connection.execute("DELETE FROM winners WHERE chat_id = ?", (chat_id,))
        connection.execute(
            "DELETE FROM settings WHERE chat_id = ?", (chat_id,)
        )
        connection.execute("DELETE FROM users WHERE chat_id = ?", (chat_id,))
        connection.execute("DELETE FROM chats WHERE chat_id = ?", (chat_id,))
        connection.execute(
            """
            INSERT INTO admin_audit (
                admin_id, chat_id, action, details, created_at
            )
            VALUES (?, ?, 'reset_chat', ?, ?)
            """,
            (
                admin_id,
                chat_id,
                "all chat data deleted",
                current_time,
            ),
        )
    return ResetResult(chat_id=chat_id)
