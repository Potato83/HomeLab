from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from potato_bot.services.economy import (
    InsufficientFunds,
    admin_adjust,
    find_reconciliation_mismatches,
    get_balance,
    reconcile_account,
    transfer,
    validate_bet,
)


def test_transfer_is_atomic_and_conserves_supply(funded_db: Path) -> None:
    sender, receiver = transfer(
        -100,
        1,
        2,
        40,
        operation_key="transfer:1",
        db_path=funded_db,
    )

    assert (sender, receiver) == (60, 140)
    assert sum(
        get_balance(-100, user_id, db_path=funded_db)
        for user_id in (1, 2, 3)
    ) == 300
    assert reconcile_account(-100, 1, db_path=funded_db) == (60, 60)
    assert reconcile_account(-100, 2, db_path=funded_db) == (140, 140)
    assert find_reconciliation_mismatches(db_path=funded_db) == []


def test_duplicate_transfer_key_is_idempotent(funded_db: Path) -> None:
    for _ in range(2):
        transfer(
            -100,
            1,
            2,
            25,
            operation_key="transfer:duplicate",
            db_path=funded_db,
        )

    assert get_balance(-100, 1, db_path=funded_db) == 75
    assert get_balance(-100, 2, db_path=funded_db) == 125


def test_concurrent_spends_cannot_overdraw(funded_db: Path) -> None:
    def spend(index: int) -> str:
        try:
            transfer(
                -100,
                1,
                2 + index,
                80,
                operation_key=f"concurrent:{index}",
                db_path=funded_db,
            )
            return "ok"
        except InsufficientFunds:
            return "denied"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = sorted(executor.map(spend, (0, 1)))

    assert results == ["denied", "ok"]
    assert get_balance(-100, 1, db_path=funded_db) == 20
    assert all(
        get_balance(-100, user_id, db_path=funded_db) >= 0
        for user_id in (1, 2, 3)
    )


@pytest.mark.parametrize("bet", (0, -1, 1_000_001))
def test_bet_boundaries_are_enforced(bet: int) -> None:
    with pytest.raises(Exception):
        validate_bet(bet)


def test_admin_adjustment_is_idempotent(db_path: Path) -> None:
    for _ in range(2):
        admin_adjust(
            1,
            1,
            50,
            admin_id=9,
            operation_key="admin:one",
            db_path=db_path,
        )
    assert get_balance(1, 1, db_path=db_path) == 50
