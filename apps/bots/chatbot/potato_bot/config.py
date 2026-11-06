from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SQLITE_MAX_INTEGER = 9_223_372_036_854_775_807
load_dotenv(PROJECT_ROOT / ".env")


def get_bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default

    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(
        f"{name} must be one of: true/false, 1/0, yes/no, on/off"
    )


def get_int_env(
    name: str,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    raw_value = os.getenv(name)
    try:
        value = default if raw_value is None else int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc

    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be at most {maximum}")
    return value


def get_path_env(name: str, default: Path) -> Path:
    raw_value = os.getenv(name)
    path = Path(raw_value).expanduser() if raw_value else default
    return path.resolve()


BOT_TOKEN = os.getenv("BOT_TOKEN")
PROXY_URL = os.getenv("PROXY_URL") or None
MY_ID = get_int_env("MY_ID", 0, minimum=0)


def get_admin_ids() -> frozenset[int]:
    raw_value = os.getenv("ADMIN_IDS")
    if raw_value is None or not raw_value.strip():
        return frozenset({MY_ID}) if MY_ID else frozenset()
    try:
        ids = [int(part.strip()) for part in raw_value.split(",")]
    except ValueError as exc:
        raise ValueError("ADMIN_IDS must be comma-separated positive integers") from exc
    if not ids or any(admin_id <= 0 for admin_id in ids):
        raise ValueError("ADMIN_IDS must be comma-separated positive integers")
    return frozenset(ids)


ADMIN_IDS = get_admin_ids()

DATABASE_PATH = get_path_env("DATABASE_PATH", PROJECT_ROOT / "bot_database.db")

MIN_BET = get_int_env("MIN_BET", 1, minimum=1)
MAX_BET = get_int_env(
    "MAX_BET",
    1_000_000,
    minimum=1,
    maximum=SQLITE_MAX_INTEGER // 36,
)
MAX_BALANCE = get_int_env(
    "MAX_BALANCE",
    9_000_000_000_000_000,
    minimum=1_000_000,
    maximum=SQLITE_MAX_INTEGER,
)
GAME_TTL_SECONDS = get_int_env(
    "GAME_TTL_SECONDS",
    15 * 60,
    minimum=60,
    maximum=24 * 60 * 60,
)

# TLS verification is enabled by default. It can be disabled only for the
# bot's own Telegram HTTP session when a local VPN/proxy requires it.
TLS_VERIFY = get_bool_env("TLS_VERIFY", True)
TLS_CA_FILE = os.getenv("TLS_CA_FILE") or None


def validate_runtime_config() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not configured")
    if MAX_BET < MIN_BET:
        raise RuntimeError("MAX_BET must be greater than or equal to MIN_BET")
    if MAX_BET > MAX_BALANCE:
        raise RuntimeError("MAX_BET must not exceed MAX_BALANCE")
    if (
        TLS_VERIFY
        and TLS_CA_FILE
        and not Path(TLS_CA_FILE).expanduser().is_file()
    ):
        raise RuntimeError(f"TLS_CA_FILE does not exist: {TLS_CA_FILE}")
