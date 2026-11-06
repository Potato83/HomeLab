from __future__ import annotations

import secrets
from dataclasses import dataclass
from pathlib import Path

from ..database import connect

RNG = secrets.SystemRandom()


@dataclass(frozen=True, slots=True)
class PlayerSummary:
    user_id: int
    full_name: str
    prefix: str | None
    amount: int
    wins: int
    games: int


@dataclass(frozen=True, slots=True)
class DailyWinner:
    name: str
    already_selected: bool


def list_users(
    chat_id: int,
    *,
    db_path: str | Path | None = None,
) -> list[tuple[int, str]]:
    with connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT user_id, full_name
            FROM users
            WHERE chat_id = ? AND user_id != 777000
            ORDER BY user_id
            """,
            (chat_id,),
        ).fetchall()
        return [
            (int(row["user_id"]), row["full_name"] or str(row["user_id"]))
            for row in rows
        ]


def top_players(
    chat_id: int,
    *,
    limit: int = 10,
    db_path: str | Path | None = None,
) -> list[PlayerSummary]:
    with connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT
                users.user_id,
                users.prefix,
                users.full_name,
                potatoes.amount,
                potatoes.wins,
                potatoes.games
            FROM potatoes
            JOIN users
              ON potatoes.user_id = users.user_id
             AND potatoes.chat_id = users.chat_id
            WHERE potatoes.chat_id = ?
            ORDER BY potatoes.amount DESC, users.user_id
            LIMIT ?
            """,
            (chat_id, limit),
        ).fetchall()
        return [
            PlayerSummary(
                user_id=int(row["user_id"]),
                full_name=row["full_name"] or str(row["user_id"]),
                prefix=row["prefix"],
                amount=int(row["amount"]),
                wins=int(row["wins"]),
                games=int(row["games"]),
            )
            for row in rows
        ]


def get_names(
    chat_id: int,
    user_ids: tuple[int, ...],
    *,
    db_path: str | Path | None = None,
) -> dict[int, str]:
    if not user_ids:
        return {}
    with connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT user_id, full_name
            FROM users
            WHERE chat_id = ?
            """,
            (chat_id,),
        ).fetchall()
        selected_ids = set(user_ids)
        names = {
            int(row["user_id"]): row["full_name"] or str(row["user_id"])
            for row in rows
            if int(row["user_id"]) in selected_ids
        }
    for user_id in user_ids:
        names.setdefault(user_id, str(user_id))
    return names


def select_daily_winner(
    chat_id: int,
    today: str,
    *,
    db_path: str | Path | None = None,
) -> DailyWinner | None:
    with connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            """
            SELECT winner_name, last_date
            FROM winners
            WHERE chat_id = ?
            """,
            (chat_id,),
        ).fetchone()
        if existing and existing["last_date"] == today:
            return DailyWinner(
                name=existing["winner_name"],
                already_selected=True,
            )

        users = connection.execute(
            """
            SELECT full_name
            FROM users
            WHERE chat_id = ? AND user_id != 777000
            """,
            (chat_id,),
        ).fetchall()
        if len(users) < 2:
            return None
        winner = RNG.choice(users)["full_name"]
        connection.execute(
            """
            INSERT INTO winners (chat_id, winner_name, last_date)
            VALUES (?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                winner_name = excluded.winner_name,
                last_date = excluded.last_date
            """,
            (chat_id, winner, today),
        )
        return DailyWinner(name=winner, already_selected=False)
