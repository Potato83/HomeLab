from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from aiogram import Bot

from ..database import DEFAULT_DB_PATH, connect
from .economy import _balance, _change_balance

LOAN_PRINCIPAL = 50
LOAN_INTEREST_PERCENT = 10
LOAN_DUE_AMOUNT = LOAN_PRINCIPAL * (100 + LOAN_INTEREST_PERCENT) // 100
LOAN_TERM_SECONDS = 30 * 60
LOAN_COOLDOWN_SECONDS = 60 * 60
LOAN_WORKER_INTERVAL_SECONDS = 30

logger = logging.getLogger(__name__)
LOAN_NOTIFICATION_LOCK = asyncio.Lock()


@dataclass(frozen=True, slots=True)
class LoanDecision:
    status: Literal["granted", "active", "cooldown", "debt", "processed"]
    balance: int
    due_at: int | None = None
    available_at: int | None = None

    def remaining_minutes(self, now: int) -> int:
        timestamp = self.due_at or self.available_at or now
        return max(1, math.ceil((timestamp - now) / 60))


@dataclass(frozen=True, slots=True)
class LoanSettlement:
    chat_id: int
    user_id: int
    due_amount: int
    balance: int
    user_name: str


def _settle_active_loan(
    connection,
    loan,
    settled_at: int,
) -> LoanSettlement | None:
    updated = connection.execute(
        """
        UPDATE loans
        SET status = 'settled', settled_at = ?
        WHERE id = ? AND status = 'active'
        """,
        (settled_at, loan["id"]),
    )
    if updated.rowcount != 1:
        return None

    balance = _change_balance(
        connection,
        operation_key=f"loan:{loan['id']}:settlement",
        chat_id=int(loan["chat_id"]),
        user_id=int(loan["user_id"]),
        delta=-int(loan["due_amount"]),
        kind="loan_repayment",
        reference_id=str(loan["id"]),
        allow_debt=True,
    )
    user = connection.execute(
        "SELECT full_name FROM users WHERE chat_id = ? AND user_id = ?",
        (loan["chat_id"], loan["user_id"]),
    ).fetchone()
    user_name = user["full_name"] if user and user["full_name"] else str(loan["user_id"])
    connection.execute(
        """
        INSERT OR IGNORE INTO loan_notifications (
            loan_id, chat_id, user_id, due_amount, balance,
            user_name, next_attempt_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            loan["id"],
            loan["chat_id"],
            loan["user_id"],
            loan["due_amount"],
            balance.balance,
            user_name,
            settled_at,
        ),
    )

    return LoanSettlement(
        chat_id=loan["chat_id"],
        user_id=loan["user_id"],
        due_amount=loan["due_amount"],
        balance=balance.balance,
        user_name=user_name,
    )


def issue_loan(
    chat_id: int,
    user_id: int,
    *,
    operation_key: str | None = None,
    now: int | None = None,
    db_path: str | Path | None = None,
) -> LoanDecision:
    current_time = int(time.time()) if now is None else now
    database_path = db_path or DEFAULT_DB_PATH

    with connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")

        if operation_key is not None:
            previous = connection.execute(
                """
                SELECT chat_id, user_id
                FROM loans WHERE request_key = ?
                """,
                (operation_key,),
            ).fetchone()
            if previous:
                if (
                    int(previous["chat_id"]) != chat_id
                    or int(previous["user_id"]) != user_id
                ):
                    raise ValueError("loan request key payload mismatch")
                return LoanDecision(
                    status="processed",
                    balance=_balance(connection, chat_id, user_id),
                )

        active_loan = connection.execute(
            """
            SELECT id, chat_id, user_id, due_amount, due_at
            FROM loans
            WHERE chat_id = ? AND user_id = ? AND status = 'active'
            """,
            (chat_id, user_id),
        ).fetchone()

        if active_loan and active_loan["due_at"] <= current_time:
            _settle_active_loan(connection, active_loan, current_time)
            active_loan = None

        balance = _balance(connection, chat_id, user_id)

        if active_loan:
            return LoanDecision(
                status="active",
                balance=balance,
                due_at=active_loan["due_at"],
            )

        if balance < 0:
            return LoanDecision(status="debt", balance=balance)

        previous_loan = connection.execute(
            """
            SELECT next_loan_at
            FROM loans
            WHERE chat_id = ? AND user_id = ?
            ORDER BY issued_at DESC, id DESC
            LIMIT 1
            """,
            (chat_id, user_id),
        ).fetchone()
        if previous_loan and previous_loan["next_loan_at"] > current_time:
            return LoanDecision(
                status="cooldown",
                balance=balance,
                available_at=previous_loan["next_loan_at"],
            )

        due_at = current_time + LOAN_TERM_SECONDS
        next_loan_at = due_at + LOAN_COOLDOWN_SECONDS
        inserted = connection.execute(
            """
            INSERT INTO loans (
                chat_id, user_id, request_key, principal, due_amount,
                issued_at, due_at, next_loan_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chat_id,
                user_id,
                operation_key,
                LOAN_PRINCIPAL,
                LOAN_DUE_AMOUNT,
                current_time,
                due_at,
                next_loan_at,
            ),
        )
        loan_id = int(inserted.lastrowid)
        credited = _change_balance(
            connection,
            operation_key=f"loan:{loan_id}:principal",
            chat_id=chat_id,
            user_id=user_id,
            delta=LOAN_PRINCIPAL,
            kind="loan_principal",
            reference_id=str(loan_id),
        )

        return LoanDecision(
            status="granted",
            balance=credited.balance,
            due_at=due_at,
            available_at=next_loan_at,
        )


def settle_due_loans(
    *,
    now: int | None = None,
    db_path: str | Path | None = None,
    limit: int = 100,
) -> list[LoanSettlement]:
    current_time = int(time.time()) if now is None else now
    database_path = db_path or DEFAULT_DB_PATH

    with connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        loans = connection.execute(
            """
            SELECT id, chat_id, user_id, due_amount, due_at
            FROM loans
            WHERE status = 'active' AND due_at <= ?
            ORDER BY due_at, id
            LIMIT ?
            """,
            (current_time, limit),
        ).fetchall()

        settlements = [
            settlement
            for loan in loans
            if (settlement := _settle_active_loan(connection, loan, current_time))
            is not None
        ]

    return settlements


def pending_loan_notifications(
    *,
    now: int | None = None,
    db_path: str | Path | None = None,
    limit: int = 100,
) -> list[tuple[int, LoanSettlement]]:
    current_time = int(time.time()) if now is None else now
    with connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT id, chat_id, user_id, due_amount, balance, user_name
            FROM loan_notifications
            WHERE sent_at IS NULL AND next_attempt_at <= ?
            ORDER BY next_attempt_at, id
            LIMIT ?
            """,
            (current_time, limit),
        ).fetchall()
    return [
        (
            int(row["id"]),
            LoanSettlement(
                chat_id=int(row["chat_id"]),
                user_id=int(row["user_id"]),
                due_amount=int(row["due_amount"]),
                balance=int(row["balance"]),
                user_name=row["user_name"],
            ),
        )
        for row in rows
    ]


def get_pending_loan_notification(
    notification_id: int,
    *,
    now: int | None = None,
    db_path: str | Path | None = None,
) -> LoanSettlement | None:
    current_time = int(time.time()) if now is None else now
    with connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT chat_id, user_id, due_amount, balance, user_name
            FROM loan_notifications
            WHERE id = ? AND sent_at IS NULL AND next_attempt_at <= ?
            """,
            (notification_id, current_time),
        ).fetchone()
    if row is None:
        return None
    return LoanSettlement(
        chat_id=int(row["chat_id"]),
        user_id=int(row["user_id"]),
        due_amount=int(row["due_amount"]),
        balance=int(row["balance"]),
        user_name=row["user_name"],
    )


def mark_loan_notification_sent(
    notification_id: int,
    *,
    now: int | None = None,
    db_path: str | Path | None = None,
) -> None:
    current_time = int(time.time()) if now is None else now
    with connect(db_path) as connection:
        connection.execute(
            """
            UPDATE loan_notifications SET sent_at = ?
            WHERE id = ? AND sent_at IS NULL
            """,
            (current_time, notification_id),
        )


def retry_loan_notification(
    notification_id: int,
    *,
    delay_seconds: int | None = None,
    now: int | None = None,
    db_path: str | Path | None = None,
) -> None:
    current_time = int(time.time()) if now is None else now
    with connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT attempts FROM loan_notifications
            WHERE id = ? AND sent_at IS NULL
            """,
            (notification_id,),
        ).fetchone()
        if row is None:
            return
        delay = (
            max(1, delay_seconds)
            if delay_seconds is not None
            else min(3600, 30 * 2 ** min(int(row["attempts"]), 7))
        )
        connection.execute(
            """
            UPDATE loan_notifications
            SET attempts = attempts + 1, next_attempt_at = ?
            WHERE id = ? AND sent_at IS NULL
            """,
            (current_time + delay, notification_id),
        )


async def _notify_settlement(
    bot: Bot,
    settlement: LoanSettlement,
) -> int | None:
    if settlement.balance < 0:
        message = (
            f"🏦 Срок займа истёк: с {settlement.user_name} списано "
            f"{settlement.due_amount} 🥔.\n"
            f"Баланс: {settlement.balance} 🥔. Новый займ будет доступен "
            "после погашения долга и окончания таймера."
        )
    else:
        message = (
            f"🏦 {settlement.user_name} вернул займ с процентами: "
            f"списано {settlement.due_amount} 🥔."
        )

    try:
        await bot.send_message(settlement.chat_id, message)
        return 0
    except Exception as exc:
        logger.exception(
            "Failed to send loan settlement notification",
            extra={
                "chat_id": settlement.chat_id,
                "user_id": settlement.user_id,
            },
        )
        retry_after = getattr(exc, "retry_after", None)
        return max(1, int(retry_after)) if retry_after is not None else None


async def run_loan_worker(bot: Bot) -> None:
    while True:
        try:
            await asyncio.to_thread(settle_due_loans)
            notifications = await asyncio.to_thread(
                pending_loan_notifications
            )
            for notification_id, settlement in notifications:
                async with LOAN_NOTIFICATION_LOCK:
                    current = await asyncio.to_thread(
                        get_pending_loan_notification,
                        notification_id,
                    )
                    if current is None:
                        continue
                    retry_after = await _notify_settlement(bot, current)
                    if retry_after == 0:
                        await asyncio.to_thread(
                            mark_loan_notification_sent,
                            notification_id,
                        )
                    else:
                        await asyncio.to_thread(
                            retry_loan_notification,
                            notification_id,
                            delay_seconds=retry_after,
                        )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Loan worker iteration failed")

        await asyncio.sleep(LOAN_WORKER_INTERVAL_SECONDS)
