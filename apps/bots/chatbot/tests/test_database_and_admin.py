from __future__ import annotations

from pathlib import Path

import pytest

from chatbot.database import connect, init_db, save_user_data
from chatbot.services.admin_ops import (
    InvalidConfirmation,
    create_reset_confirmation,
    reset_chat,
)
from chatbot.services.economy import admin_adjust, get_balance


def test_reset_deletes_all_chat_data(db_path: Path) -> None:
    save_user_data(1, 2, "User", "Chat", "group", db_path)
    admin_adjust(
        1,
        2,
        50,
        admin_id=99,
        operation_key="grant",
        db_path=db_path,
    )
    token = create_reset_confirmation(1, 99, now=100, db_path=db_path)
    result = reset_chat(token, 99, now=101, db_path=db_path)

    assert result.chat_id == 1
    assert get_balance(1, 2, db_path=db_path) == 0
    with connect(db_path) as connection:
        assert connection.execute(
            "SELECT 1 FROM users WHERE chat_id = 1"
        ).fetchone() is None
        assert connection.execute(
            "SELECT 1 FROM chats WHERE chat_id = 1"
        ).fetchone() is None
    with pytest.raises(InvalidConfirmation):
        reset_chat(token, 99, now=102, db_path=db_path)
