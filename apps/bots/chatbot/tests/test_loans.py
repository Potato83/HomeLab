import sqlite3
from concurrent.futures import ThreadPoolExecutor

from potato_bot.database import init_db
from potato_bot.services.economy import reconcile_account
from potato_bot.services.loans import (
    LOAN_COOLDOWN_SECONDS,
    LOAN_DUE_AMOUNT,
    LOAN_PRINCIPAL,
    LOAN_TERM_SECONDS,
    issue_loan,
    settle_due_loans,
)


def get_balance(db_path, chat_id=1, user_id=2):
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT amount FROM potatoes WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        ).fetchone()
    return row[0]


def test_loan_grants_principal_and_collects_principal_plus_interest(tmp_path):
    db_path = tmp_path / "bot.db"
    init_db(db_path)

    decision = issue_loan(1, 2, now=1_000, db_path=db_path)

    assert decision.status == "granted"
    assert decision.balance == LOAN_PRINCIPAL
    assert decision.due_at == 1_000 + LOAN_TERM_SECONDS
    assert get_balance(db_path) == LOAN_PRINCIPAL

    settlements = settle_due_loans(
        now=1_000 + LOAN_TERM_SECONDS,
        db_path=db_path,
    )

    assert len(settlements) == 1
    assert settlements[0].due_amount == LOAN_DUE_AMOUNT
    assert settlements[0].balance == LOAN_PRINCIPAL - LOAN_DUE_AMOUNT
    assert get_balance(db_path) == -5
    assert reconcile_account(1, 2, db_path=db_path) == (-5, -5)


def test_due_loan_is_settled_only_once(tmp_path):
    db_path = tmp_path / "bot.db"
    init_db(db_path)
    issue_loan(1, 2, now=1_000, db_path=db_path)

    first = settle_due_loans(
        now=1_000 + LOAN_TERM_SECONDS,
        db_path=db_path,
    )
    second = settle_due_loans(
        now=1_000 + LOAN_TERM_SECONDS + 1,
        db_path=db_path,
    )

    assert len(first) == 1
    assert second == []
    assert get_balance(db_path) == -5


def test_transferred_loan_does_not_create_currency(tmp_path):
    db_path = tmp_path / "bot.db"
    init_db(db_path)
    issue_loan(1, 2, now=1_000, db_path=db_path)

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE potatoes SET amount = amount - 50 "
            "WHERE chat_id = 1 AND user_id = 2"
        )
        connection.execute(
            "INSERT INTO potatoes (chat_id, user_id, amount) VALUES (1, 3, 50)"
        )

    settle_due_loans(
        now=1_000 + LOAN_TERM_SECONDS,
        db_path=db_path,
    )

    assert get_balance(db_path, user_id=2) == -LOAN_DUE_AMOUNT
    assert get_balance(db_path, user_id=3) == LOAN_PRINCIPAL
    assert get_balance(db_path, user_id=2) + get_balance(db_path, user_id=3) == -5


def test_active_loan_and_cooldown_block_reborrowing(tmp_path):
    db_path = tmp_path / "bot.db"
    init_db(db_path)
    issue_loan(1, 2, now=1_000, db_path=db_path)

    active = issue_loan(1, 2, now=1_001, db_path=db_path)
    assert active.status == "active"

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE potatoes SET amount = 100 WHERE chat_id = 1 AND user_id = 2"
        )

    due_at = 1_000 + LOAN_TERM_SECONDS
    settle_due_loans(now=due_at, db_path=db_path)
    cooldown = issue_loan(1, 2, now=due_at + 1, db_path=db_path)

    assert cooldown.status == "cooldown"
    assert cooldown.available_at == due_at + LOAN_COOLDOWN_SECONDS


def test_negative_balance_must_be_repaid_before_next_loan(tmp_path):
    db_path = tmp_path / "bot.db"
    init_db(db_path)
    issue_loan(1, 2, now=1_000, db_path=db_path)

    due_at = 1_000 + LOAN_TERM_SECONDS
    settle_due_loans(now=due_at, db_path=db_path)
    after_cooldown = issue_loan(
        1,
        2,
        now=due_at + LOAN_COOLDOWN_SECONDS,
        db_path=db_path,
    )

    assert after_cooldown.status == "debt"
    assert after_cooldown.balance == -5


def test_loan_state_survives_reopening_database(tmp_path):
    db_path = tmp_path / "bot.db"
    init_db(db_path)
    issue_loan(1, 2, now=1_000, db_path=db_path)

    init_db(db_path)
    settlements = settle_due_loans(
        now=1_000 + LOAN_TERM_SECONDS,
        db_path=db_path,
    )

    assert len(settlements) == 1
    assert get_balance(db_path) == -5


def test_concurrent_requests_grant_only_one_loan(tmp_path):
    db_path = tmp_path / "bot.db"
    init_db(db_path)

    with ThreadPoolExecutor(max_workers=2) as executor:
        decisions = list(
            executor.map(
                lambda _: issue_loan(1, 2, now=1_000, db_path=db_path),
                range(2),
            )
        )

    assert sorted(decision.status for decision in decisions) == [
        "active",
        "granted",
    ]
    assert get_balance(db_path) == LOAN_PRINCIPAL
