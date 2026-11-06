from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import types
from aiogram.exceptions import TelegramBadRequest

from .. import config

MOSCOW_TIMEZONE = ZoneInfo("Europe/Moscow")


async def edit_text_if_changed(
    message: types.Message,
    text: str,
    **kwargs: Any,
) -> None:
    try:
        await message.edit_text(text, **kwargs)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


def get_moscow_today() -> str:
    return datetime.now(MOSCOW_TIMEZONE).strftime("%Y-%m-%d")


def get_user_info(message: types.Message) -> tuple[int, str]:
    if message.sender_chat:
        return message.sender_chat.id, message.sender_chat.title
    if message.from_user:
        return message.from_user.id, message.from_user.full_name
    raise ValueError("message has no identifiable sender")


def real_user_id(message: types.Message) -> int | None:
    if message.sender_chat or not message.from_user or message.from_user.is_bot:
        return None
    return message.from_user.id


def parse_bet(text: str | None, balance: int | None = None) -> int:
    """Return a valid integer bet, or zero for malformed/unsafe input."""
    args = text.split() if text else []
    if len(args) < 2 or not args[1].isdigit():
        return 0
    try:
        bet = int(args[1])
    except ValueError:
        return 0
    if (
        bet < config.MIN_BET
        or bet > config.MAX_BET
        or (balance is not None and bet > balance)
    ):
        return 0
    return bet


def game_callback_payload(data: str | None, prefix: str) -> str:
    """Remove only the action prefix: persisted game IDs may contain colons."""
    if not isinstance(data, str) or not data.startswith(prefix):
        raise ValueError("invalid game callback prefix")
    payload = data[len(prefix):]
    if not payload or len(data.encode("utf-8")) > 64:
        raise ValueError("invalid game callback payload")
    return payload


def blackjack_callback(data: str | None, prefix: str) -> tuple[str, int]:
    game_id, version = game_callback_payload(data, prefix).rsplit(":", 1)
    if not game_id or not version.isascii() or not version.isdecimal():
        raise ValueError("invalid blackjack callback version")
    return game_id, int(version)
