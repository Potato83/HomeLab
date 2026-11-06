from __future__ import annotations

from pathlib import Path

import pytest

from chatbot.database import init_db, save_user_data
from chatbot.services.economy import admin_adjust


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "bot.db"
    init_db(path)
    return path


@pytest.fixture
def funded_db(db_path: Path) -> Path:
    for user_id in (1, 2, 3):
        save_user_data(
            -100,
            user_id,
            f"User <{user_id}>",
            "Test chat",
            "supergroup",
            db_path,
        )
        admin_adjust(
            -100,
            user_id,
            100,
            admin_id=999,
            operation_key=f"fixture:grant:{user_id}",
            db_path=db_path,
        )
    return db_path
