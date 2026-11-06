from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from .. import config
from ..database import connect

SHOP_CATALOG = {
    "shield": ("Щит", 50),
    "title": ("Лицензия на титул", 200),
    "insurance": ("Страховка казино", 100),
}
logger = logging.getLogger(__name__)


class EconomyError(Exception):
    """Base class for expected economy rule failures."""


class InvalidAmount(EconomyError):
    pass


class InsufficientFunds(EconomyError):
    def __init__(self, balance: int, required: int) -> None:
        super().__init__("insufficient funds")
        self.balance = balance
        self.required = required


class BalanceLimitExceeded(EconomyError):
    pass


class CooldownActive(EconomyError):
    def __init__(self, available_at: int) -> None:
        super().__init__("cooldown is active")
        self.available_at = available_at


class ItemUnavailable(EconomyError):
    pass


@dataclass(frozen=True, slots=True)
class ChangeResult:
    balance: int
    applied: bool


@dataclass(frozen=True, slots=True)
class DigResult:
    mined: int
    balance: int
    available_at: int


@dataclass(frozen=True, slots=True)
class SleepCharge:
    price: int
    duration_minutes: int
    shield_used: bool
    operation_key: str
    until_date: int
    extended: bool
    status: str


def _sleep_charge(row) -> SleepCharge:
    return SleepCharge(
        price=int(row["price"]),
        duration_minutes=int(row["duration_minutes"]),
        shield_used=bool(row["shield_used"]),
        operation_key=row["operation_key"],
        until_date=int(row["until_date"]),
        extended=bool(row["extended"]),
        status=row["status"],
    )


def validate_amount(
    amount: int,
    *,
    minimum: int = 1,
    maximum: int | None = None,
) -> int:
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise InvalidAmount("amount must be an integer")
    upper_bound = config.MAX_BALANCE if maximum is None else maximum
    if amount < minimum or amount > upper_bound:
        raise InvalidAmount(
            f"amount must be between {minimum} and {upper_bound}"
        )
    return amount


def validate_bet(bet: int) -> int:
    return validate_amount(
        bet,
        minimum=config.MIN_BET,
        maximum=config.MAX_BET,
    )


def _ensure_account(connection, chat_id: int, user_id: int) -> None:
    connection.execute(
        """
        INSERT OR IGNORE INTO potatoes (
            chat_id, user_id, amount, last_dig_date, wins, games
        )
        VALUES (?, ?, 0, 0, 0, 0)
        """,
        (chat_id, user_id),
    )


def _balance(connection, chat_id: int, user_id: int) -> int:
    _ensure_account(connection, chat_id, user_id)
    row = connection.execute(
        "SELECT amount FROM potatoes WHERE chat_id = ? AND user_id = ?",
        (chat_id, user_id),
    ).fetchone()
    return int(row["amount"])


def _reserved_balance_headroom(
    connection,
    chat_id: int,
    user_id: int,
) -> int:
    # Stakes are already debited; keep room for their full possible payout.
    row = connection.execute(
        """
        SELECT COALESCE(SUM(
            CASE WHEN challenger_id = ? AND challenger_reserved = 1
                 THEN bet * 2 ELSE 0 END
          + CASE WHEN target_id = ? AND target_reserved = 1
                 THEN bet * 2 ELSE 0 END
        ), 0) AS reserved
        FROM games
        WHERE chat_id = ? AND status IN ('pending', 'active')
        """,
        (user_id, user_id, chat_id),
    ).fetchone()
    game_reserve = int(row["reserved"])
    sleep_row = connection.execute(
        """
        SELECT COALESCE(SUM(price), 0) AS reserved
        FROM sleep_actions
        WHERE chat_id = ? AND attacker_id = ? AND status = 'pending'
        """,
        (chat_id, user_id),
    ).fetchone()
    # A pending sleep action can still be refunded if Telegram rejects it.
    return game_reserve + int(sleep_row["reserved"])


def _ensure_balance_headroom(
    connection,
    chat_id: int,
    user_id: int,
    additional_credit: int,
) -> None:
    # additional_credit is the maximum net gain from the action being started.
    balance = _balance(connection, chat_id, user_id)
    if (
        balance
        + _reserved_balance_headroom(connection, chat_id, user_id)
        + additional_credit
        > config.MAX_BALANCE
    ):
        raise BalanceLimitExceeded("balance limit leaves no game payout room")


def _change_balance(
    connection,
    *,
    operation_key: str,
    chat_id: int,
    user_id: int,
    delta: int,
    kind: str,
    reference_id: str | None = None,
    allow_debt: bool = False,
) -> ChangeResult:
    if isinstance(delta, bool) or not isinstance(delta, int):
        raise InvalidAmount("delta must be an integer")
    existing = connection.execute(
        """
        SELECT chat_id, user_id, delta, balance_after, kind, reference_id
        FROM operations
        WHERE operation_key = ?
        """,
        (operation_key,),
    ).fetchone()
    if existing:
        expected = (chat_id, user_id, delta, kind, reference_id)
        previous = (
            int(existing["chat_id"]),
            int(existing["user_id"]),
            int(existing["delta"]),
            existing["kind"],
            existing["reference_id"],
        )
        if previous != expected:
            raise ValueError(
                "operation key was reused for another balance change"
            )
        return ChangeResult(balance=int(existing["balance_after"]), applied=False)

    current = _balance(connection, chat_id, user_id)
    updated = current + delta
    # Credits must be allowed to reduce an existing debt. Only reject a debit
    # when it would leave an account negative, unless the caller explicitly
    # allows debt (loan settlement or owner adjustment).
    if delta < 0 and not allow_debt and updated < 0:
        raise InsufficientFunds(balance=current, required=-delta)
    reserved = (
        _reserved_balance_headroom(connection, chat_id, user_id)
        if delta > 0
        else 0
    )
    if (
        (delta > 0 and updated + reserved > config.MAX_BALANCE)
        or (
            updated < -config.MAX_BALANCE
            and updated <= current
        )
    ):
        raise BalanceLimitExceeded("balance limit exceeded")

    connection.execute(
        "UPDATE potatoes SET amount = ? WHERE chat_id = ? AND user_id = ?",
        (updated, chat_id, user_id),
    )
    connection.execute(
        """
        INSERT INTO operations (
            operation_key, chat_id, user_id, delta, balance_after,
            kind, reference_id, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            operation_key,
            chat_id,
            user_id,
            delta,
            updated,
            kind,
            reference_id,
            int(time.time()),
        ),
    )
    return ChangeResult(balance=updated, applied=True)


def get_balance(
    chat_id: int,
    user_id: int,
    *,
    db_path: str | Path | None = None,
) -> int:
    with connect(db_path) as connection:
        return _balance(connection, chat_id, user_id)


def transfer(
    chat_id: int,
    sender_id: int,
    receiver_id: int,
    amount: int,
    *,
    operation_key: str,
    db_path: str | Path | None = None,
) -> tuple[int, int]:
    validate_amount(amount)
    if sender_id == receiver_id:
        raise InvalidAmount("cannot transfer to self")

    with connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        reference = operation_key
        sender = _change_balance(
            connection,
            operation_key=f"{operation_key}:debit",
            chat_id=chat_id,
            user_id=sender_id,
            delta=-amount,
            kind="transfer_out",
            reference_id=reference,
        )
        receiver = _change_balance(
            connection,
            operation_key=f"{operation_key}:credit",
            chat_id=chat_id,
            user_id=receiver_id,
            delta=amount,
            kind="transfer_in",
            reference_id=reference,
        )
        return sender.balance, receiver.balance


def admin_adjust(
    chat_id: int,
    user_id: int,
    amount: int,
    *,
    admin_id: int,
    operation_key: str,
    db_path: str | Path | None = None,
) -> int:
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise InvalidAmount("amount must be an integer")
    validate_amount(abs(amount))
    with connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        result = _change_balance(
            connection,
            operation_key=operation_key,
            chat_id=chat_id,
            user_id=user_id,
            delta=amount,
            kind="admin_adjustment",
            reference_id=str(admin_id),
            allow_debt=True,
        )
        if result.applied:
            connection.execute(
                """
                INSERT INTO admin_audit (
                    admin_id, chat_id, action, details, created_at
                )
                VALUES (?, ?, 'adjust_balance', ?, ?)
                """,
                (
                    admin_id,
                    chat_id,
                    f"user_id={user_id};delta={amount}",
                    int(time.time()),
                ),
            )
        return result.balance


def dig(
    chat_id: int,
    user_id: int,
    mined: int,
    *,
    now: int | None = None,
    operation_key: str,
    db_path: str | Path | None = None,
) -> DigResult:
    validate_amount(mined, maximum=10)
    current_time = int(time.time()) if now is None else now

    with connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        _ensure_account(connection, chat_id, user_id)
        settings = connection.execute(
            "SELECT dig_cd FROM settings WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
        cooldown_seconds = int(settings["dig_cd"] if settings else 24) * 3600
        row = connection.execute(
            """
            SELECT last_dig_date
            FROM potatoes
            WHERE chat_id = ? AND user_id = ?
            """,
            (chat_id, user_id),
        ).fetchone()
        last_dig = int(row["last_dig_date"] or 0)
        available_at = last_dig + cooldown_seconds

        previous = connection.execute(
            """
            SELECT chat_id, user_id, delta, kind
            FROM operations
            WHERE operation_key = ?
            """,
            (operation_key,),
        ).fetchone()
        if previous:
            if (
                int(previous["chat_id"]) != chat_id
                or int(previous["user_id"]) != user_id
                or previous["kind"] != "dig_reward"
            ):
                raise ValueError("operation key was reused for another action")
            return DigResult(
                mined=int(previous["delta"]),
                balance=_balance(connection, chat_id, user_id),
                available_at=available_at,
            )

        if current_time < available_at:
            raise CooldownActive(available_at)

        result = _change_balance(
            connection,
            operation_key=operation_key,
            chat_id=chat_id,
            user_id=user_id,
            delta=mined,
            kind="dig_reward",
            reference_id=operation_key,
        )
        if result.applied:
            connection.execute(
                """
                UPDATE potatoes
                SET last_dig_date = ?
                WHERE chat_id = ? AND user_id = ?
                """,
                (current_time, chat_id, user_id),
            )
        return DigResult(
            mined=mined,
            balance=result.balance,
            available_at=current_time + cooldown_seconds,
        )


def purchase(
    chat_id: int,
    user_id: int,
    product_id: str,
    *,
    operation_key: str,
    db_path: str | Path | None = None,
) -> tuple[str, int, int]:
    product = SHOP_CATALOG.get(product_id)
    if product is None:
        raise ItemUnavailable("unknown product")
    title, price = product

    with connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        charged = _change_balance(
            connection,
            operation_key=f"{operation_key}:charge",
            chat_id=chat_id,
            user_id=user_id,
            delta=-price,
            kind="shop_purchase",
            reference_id=product_id,
        )
        if charged.applied:
            connection.execute(
                """
                INSERT INTO inventory (chat_id, user_id, item_type, amount)
                VALUES (?, ?, ?, 1)
                ON CONFLICT(chat_id, user_id, item_type)
                DO UPDATE SET amount = amount + 1
                """,
                (chat_id, user_id, product_id),
            )
        return title, price, charged.balance


def charge_sleep(
    chat_id: int,
    attacker_id: int,
    target_id: int,
    *,
    operation_key: str,
    until_date: int,
    duration_minutes: int,
    extended: bool,
    db_path: str | Path | None = None,
) -> SleepCharge:
    if (
        isinstance(until_date, bool)
        or not isinstance(until_date, int)
        or until_date <= 0
    ):
        raise InvalidAmount("invalid sleep expiration")
    if (
        isinstance(duration_minutes, bool)
        or not isinstance(duration_minutes, int)
        or duration_minutes <= 0
    ):
        raise InvalidAmount("invalid sleep duration")
    with connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT * FROM sleep_actions WHERE operation_key = ?",
            (operation_key,),
        ).fetchone()
        if existing:
            if (
                int(existing["chat_id"]) != chat_id
                or int(existing["attacker_id"]) != attacker_id
                or int(existing["target_id"]) != target_id
            ):
                raise ValueError("sleep operation key payload mismatch")
            return _sleep_charge(existing)

        # A pending action may have reached Telegram before a network failure.
        # Keep the next paid duration after that uncertain deadline.
        requested_base = until_date - duration_minutes * 60
        pending = connection.execute(
            """
            SELECT MAX(until_date) AS until_date
            FROM sleep_actions
            WHERE chat_id = ? AND target_id = ? AND status = 'pending'
              AND until_date > ?
            """,
            (chat_id, target_id, int(time.time())),
        ).fetchone()
        if (
            pending["until_date"] is not None
            and int(pending["until_date"]) > requested_base
        ):
            until_date = int(pending["until_date"]) + duration_minutes * 60
            extended = True

        settings = connection.execute(
            """
            SELECT sleep_price
            FROM settings
            WHERE chat_id = ?
            """,
            (chat_id,),
        ).fetchone()
        price = int(settings["sleep_price"] if settings else 10)
        previous_charge = connection.execute(
            "SELECT delta FROM operations WHERE operation_key = ?",
            (f"{operation_key}:charge",),
        ).fetchone()
        if previous_charge:
            price = -int(previous_charge["delta"])
        charged = _change_balance(
            connection,
            operation_key=f"{operation_key}:charge",
            chat_id=chat_id,
            user_id=attacker_id,
            delta=-price,
            kind="sleep_charge",
            reference_id=str(target_id),
        )

        shield_used = False
        if charged.applied:
            shield = connection.execute(
                """
                UPDATE inventory
                SET amount = amount - 1
                WHERE chat_id = ? AND user_id = ?
                  AND item_type = 'shield' AND amount > 0
                """,
                (chat_id, target_id),
            )
            shield_used = shield.rowcount == 1
        else:
            shield_used = (
                connection.execute(
                    """
                    SELECT 1
                    FROM operations
                    WHERE operation_key = ?
                      AND kind = 'sleep_shield_used'
                    """,
                    (f"{operation_key}:shield",),
                ).fetchone()
                is not None
            )

        if shield_used:
            connection.execute(
                """
                INSERT OR IGNORE INTO operations (
                    operation_key, chat_id, user_id, delta, balance_after,
                    kind, reference_id, created_at
                )
                VALUES (?, ?, ?, 0, ?, 'sleep_shield_used', ?, ?)
                """,
                (
                    f"{operation_key}:shield",
                    chat_id,
                    target_id,
                    _balance(connection, chat_id, target_id),
                    str(attacker_id),
                    int(time.time()),
                ),
            )
        refunded = connection.execute(
            "SELECT 1 FROM operations WHERE operation_key = ?",
            (f"{operation_key}:refund",),
        ).fetchone()
        status = (
            "refunded" if refunded else "shield" if shield_used else "pending"
        )
        connection.execute(
            """
            INSERT INTO sleep_actions (
                operation_key, chat_id, attacker_id, target_id, price,
                duration_minutes, shield_used, extended, until_date, status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                operation_key,
                chat_id,
                attacker_id,
                target_id,
                price,
                duration_minutes,
                int(shield_used),
                int(extended),
                until_date,
                status,
            ),
        )
        return SleepCharge(
            price=price,
            duration_minutes=duration_minutes,
            shield_used=shield_used,
            operation_key=operation_key,
            until_date=until_date,
            extended=extended,
            status=status,
        )


def get_sleep_action(
    operation_key: str,
    *,
    db_path: str | Path | None = None,
) -> SleepCharge | None:
    with connect(db_path) as connection:
        row = connection.execute(
            "SELECT * FROM sleep_actions WHERE operation_key = ?",
            (operation_key,),
        ).fetchone()
        return _sleep_charge(row) if row else None


def latest_sleep_deadline(
    chat_id: int,
    target_id: int,
    *,
    now: int | None = None,
    db_path: str | Path | None = None,
) -> int | None:
    current_time = int(time.time()) if now is None else now
    with connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT MAX(until_date) AS until_date
            FROM sleep_actions
            WHERE chat_id = ? AND target_id = ?
              AND status IN ('pending', 'applied') AND until_date > ?
            """,
            (chat_id, target_id, current_time),
        ).fetchone()
        return int(row["until_date"]) if row["until_date"] is not None else None


def pending_sleep_actions(
    *,
    limit: int = 100,
    older_than_seconds: int = 0,
    now: int | None = None,
    db_path: str | Path | None = None,
) -> list[tuple[int, int, int, SleepCharge]]:
    cutoff = (int(time.time()) if now is None else now) - older_than_seconds
    with connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT sleep_actions.*
            FROM sleep_actions
            JOIN operations
              ON operations.operation_key =
                 sleep_actions.operation_key || ':charge'
            WHERE sleep_actions.status = 'pending'
              AND operations.created_at <= ?
            ORDER BY sleep_actions.until_date, sleep_actions.operation_key
            LIMIT ?
            """,
            (cutoff, limit),
        ).fetchall()
    return [
        (
            int(row["chat_id"]),
            int(row["attacker_id"]),
            int(row["target_id"]),
            _sleep_charge(row),
        )
        for row in rows
    ]


def mark_sleep_applied(
    charge: SleepCharge,
    *,
    db_path: str | Path | None = None,
) -> None:
    with connect(db_path) as connection:
        connection.execute(
            """
            UPDATE sleep_actions SET status = 'applied'
            WHERE operation_key = ? AND status = 'pending'
            """,
            (charge.operation_key,),
        )


def refund_sleep(
    chat_id: int,
    attacker_id: int,
    charge: SleepCharge,
    *,
    db_path: str | Path | None = None,
) -> int:
    with connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        action = connection.execute(
            "SELECT status FROM sleep_actions WHERE operation_key = ?",
            (charge.operation_key,),
        ).fetchone()
        if action is not None and action["status"] != "pending":
            return _balance(connection, chat_id, attacker_id)
        connection.execute(
            """
            UPDATE sleep_actions SET status = 'refunded'
            WHERE operation_key = ? AND status = 'pending'
            """,
            (charge.operation_key,),
        )
        result = _change_balance(
            connection,
            operation_key=f"{charge.operation_key}:refund",
            chat_id=chat_id,
            user_id=attacker_id,
            delta=charge.price,
            kind="sleep_refund",
            reference_id=charge.operation_key,
        )
        return result.balance


def get_sleep_duration(
    chat_id: int,
    *,
    db_path: str | Path | None = None,
) -> int:
    with connect(db_path) as connection:
        row = connection.execute(
            "SELECT sleep_duration FROM settings WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
        return int(row["sleep_duration"] if row else 2)


def set_title(
    chat_id: int,
    user_id: int,
    title: str,
    *,
    operation_key: str,
    db_path: str | Path | None = None,
) -> None:
    normalized = title.strip()
    if not normalized or len(normalized) > 15 or any(
        character in normalized for character in "\r\n"
    ):
        raise InvalidAmount("invalid title")

    with connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        previous = connection.execute(
            """
            SELECT chat_id, user_id, kind, reference_id
            FROM operations WHERE operation_key = ?
            """,
            (operation_key,),
        ).fetchone()
        if previous:
            if (
                int(previous["chat_id"]) != chat_id
                or int(previous["user_id"]) != user_id
                or previous["kind"] != "title_set"
                or previous["reference_id"] != normalized
            ):
                raise ValueError("title operation key payload mismatch")
            return

        consumed = connection.execute(
            """
            UPDATE inventory
            SET amount = amount - 1
            WHERE chat_id = ? AND user_id = ?
              AND item_type = 'title' AND amount > 0
            """,
            (chat_id, user_id),
        )
        if consumed.rowcount != 1:
            raise ItemUnavailable("title license is missing")
        connection.execute(
            """
            UPDATE users
            SET prefix = ?
            WHERE chat_id = ? AND user_id = ?
            """,
            (f"[{normalized}]", chat_id, user_id),
        )
        connection.execute(
            """
            INSERT INTO operations (
                operation_key, chat_id, user_id, delta, balance_after,
                kind, reference_id, created_at
            )
            VALUES (?, ?, ?, 0, ?, 'title_set', ?, ?)
            """,
            (
                operation_key,
                chat_id,
                user_id,
                _balance(connection, chat_id, user_id),
                normalized,
                int(time.time()),
            ),
        )


def remove_title(
    chat_id: int,
    user_id: int,
    *,
    db_path: str | Path | None = None,
) -> None:
    with connect(db_path) as connection:
        connection.execute(
            "UPDATE users SET prefix = NULL WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        )


def consume_insurance(connection, chat_id: int, user_id: int) -> bool:
    consumed = connection.execute(
        """
        UPDATE inventory
        SET amount = amount - 1
        WHERE chat_id = ? AND user_id = ?
          AND item_type = 'insurance' AND amount > 0
        """,
        (chat_id, user_id),
    )
    return consumed.rowcount == 1


def update_stats(
    connection,
    chat_id: int,
    user_id: int,
    *,
    won: bool,
) -> None:
    _ensure_account(connection, chat_id, user_id)
    connection.execute(
        """
        UPDATE potatoes
        SET games = games + 1,
            wins = wins + ?
        WHERE chat_id = ? AND user_id = ?
        """,
        (1 if won else 0, chat_id, user_id),
    )


def reconcile_account(
    chat_id: int,
    user_id: int,
    *,
    db_path: str | Path | None = None,
) -> tuple[int, int]:
    with connect(db_path) as connection:
        balance = _balance(connection, chat_id, user_id)
        ledger = connection.execute(
            """
            SELECT COALESCE(SUM(delta), 0) AS total
            FROM operations
            WHERE chat_id = ? AND user_id = ?
            """,
            (chat_id, user_id),
        ).fetchone()
        return balance, int(ledger["total"])


def find_reconciliation_mismatches(
    *,
    limit: int = 100,
    db_path: str | Path | None = None,
) -> list[tuple[int, int, int, int]]:
    with connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT
                potatoes.chat_id,
                potatoes.user_id,
                potatoes.amount AS balance,
                COALESCE(SUM(operations.delta), 0) AS ledger_total
            FROM potatoes
            LEFT JOIN operations
              ON operations.chat_id = potatoes.chat_id
             AND operations.user_id = potatoes.user_id
            GROUP BY potatoes.chat_id, potatoes.user_id
            HAVING potatoes.amount != COALESCE(SUM(operations.delta), 0)
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            (
                int(row["chat_id"]),
                int(row["user_id"]),
                int(row["balance"]),
                int(row["ledger_total"]),
            )
            for row in rows
        ]


async def run_reconciliation_worker(
    *,
    interval_seconds: int = 60 * 60,
) -> None:
    while True:
        try:
            mismatches = await asyncio.to_thread(
                find_reconciliation_mismatches
            )
            if mismatches:
                logger.error(
                    "Economy reconciliation mismatch",
                    extra={
                        "mismatch_count": len(mismatches),
                        "first_accounts": mismatches[:10],
                    },
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Economy reconciliation worker failed")
        await asyncio.sleep(interval_seconds)
