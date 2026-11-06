from __future__ import annotations

import asyncio
import math
import secrets
import time

from aiogram import Router, html, types
from aiogram.filters import Command

from .helpers import get_moscow_today, real_user_id
from ..services.economy import BalanceLimitExceeded, CooldownActive, dig
from ..services.profiles import select_daily_winner, top_players

router = Router()
RNG = secrets.SystemRandom()


@router.message(Command("pidor"))
async def who_is_pidor(message: types.Message) -> None:
    if message.chat.type == "private":
        await message.answer("Эту команду можно использовать только в группах!")
        return

    winner = await asyncio.to_thread(
        select_daily_winner,
        message.chat.id,
        get_moscow_today(),
    )
    if winner is None:
        await message.answer(
            "Для игры нужно хотя бы 2 человека, которые писали боту!"
        )
        return
    safe_name = html.quote(winner.name)
    if winner.already_selected:
        await message.answer(
            f"Сегодняшний pidor уже выбран! Это — {safe_name}! ✨",
            parse_mode="HTML",
        )
        return
    phrase = RNG.choice(
        (
            "Так-так... Кто же сегодня pidor? 🧐\n"
            f"Барабанная дробь...\n\nЭто {safe_name}! 🎉",
            f"Сегодня боги pidors выбрали {safe_name}! ⭐",
            f"Внимательно изучив этот чат, я заявляю: "
            f"{safe_name} — сегодня pidor! 🔥",
        )
    )
    await message.answer(phrase, parse_mode="HTML")


@router.message(Command("dig"))
async def dig_potato(message: types.Message) -> None:
    user_id = real_user_id(message)
    if user_id is None:
        await message.answer("Копать картошку могут только обычные пользователи.")
        return

    now = int(time.time())
    mined = RNG.randint(1, 10)
    try:
        result = await asyncio.to_thread(
            dig,
            message.chat.id,
            user_id,
            mined,
            now=now,
            operation_key=f"dig:{message.chat.id}:{message.message_id}",
        )
    except CooldownActive as exc:
        remaining = max(1, math.ceil((exc.available_at - now) / 60))
        await message.answer(f"Подожди ещё {remaining} мин. ⏳")
        return
    except BalanceLimitExceeded:
        await message.answer(
            "Для награды нет места до лимита с учётом незавершённых выплат."
        )
        return

    await message.answer(
        f"Ты пошёл в огород и выкопал {result.mined} 🥔!\n"
        f"Теперь у тебя в мешке: {result.balance} шт."
    )


@router.message(Command("top"))
async def top_potatoes(message: types.Message) -> None:
    players = await asyncio.to_thread(top_players, message.chat.id)
    if not players:
        await message.answer("Тут пока пусто...")
        return

    lines = ["🏆 <b>Топ богачей чата:</b>", ""]
    for index, player in enumerate(players, start=1):
        medal = ("🥇", "🥈", "🥉")[index - 1] if index <= 3 else "👤"
        name = html.quote(player.full_name)
        prefix = html.quote(player.prefix) if player.prefix else ""
        display_name = f"{prefix} {name}".strip()
        win_rate = int(player.wins / player.games * 100) if player.games else 0
        win_rate_text = f" [WR: {win_rate}%]" if player.games else ""
        lines.append(
            f"{medal} {display_name} — {player.amount} 🥔{win_rate_text}"
        )
    await message.answer("\n".join(lines), parse_mode="HTML")
