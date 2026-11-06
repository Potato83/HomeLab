from __future__ import annotations

import asyncio
import secrets

from aiogram import F, Router, html, types
from aiogram.filters import Command

from .helpers import get_user_info
from ..services.profiles import list_users

router = Router()
RNG = secrets.SystemRandom()


@router.message(Command("start"))
async def cmd_start(message: types.Message) -> None:
    _, name = get_user_info(message)
    commands = (
        "/ping и /all — позвать известных боту участников\n"
        "/pidor — ежедневный выбор игрока\n"
        "/dig — копать картошку\n"
        "/top — лидеры и винрейт\n"
        "/give — передать картошку ответом на сообщение\n"
        "/pvp — дуэль ответом на сообщение\n"
        "/rps — камень, ножницы, бумага\n"
        "/bj — блэкджек\n"
        "/flip — монетка\n"
        "/roulette — рулетка\n"
        "/shop — магазин\n"
        "/settitle — установить купленный титул\n"
        "/removetitle — удалить титул\n"
        "/sleep — отправить игрока спать\n"
        "/zaim — занять 50 🥔 с возвратом 55 🥔\n"
        "/pat — погладить ответом на сообщение"
    )
    await message.answer(
        f"Привет, {html.quote(name)}! Бот запущен.\n\nКоманды:\n{commands}",
        parse_mode="HTML",
    )


@router.message(Command("ping", "all"))
async def cmd_ping(message: types.Message) -> None:
    users = await asyncio.to_thread(list_users, message.chat.id)
    if not users:
        return

    header = "📢 <b>Общий сбор!</b>\n\n"
    mentions: list[str] = []
    chunk_size = len(header)
    for user_id, name in users:
        safe_name = html.quote(name)
        if user_id < 0:
            mention = safe_name
        else:
            mention = f'<a href="tg://user?id={user_id}">{safe_name}</a>'
        addition = len(mention) + (2 if mentions else 0)
        if mentions and chunk_size + addition > 3500:
            await message.answer(
                header + ", ".join(mentions),
                parse_mode="HTML",
            )
            mentions.clear()
            chunk_size = len(header)
            addition = len(mention)
        mentions.append(mention)
        chunk_size += addition

    if mentions:
        await message.answer(header + ", ".join(mentions), parse_mode="HTML")


@router.message(Command("pat"))
async def cmd_pat(message: types.Message) -> None:
    if not message.reply_to_message:
        await message.answer(
            "Эту команду нужно писать в ответ на сообщение того, "
            "кого хочешь погладить!"
        )
        return
    _, patter = get_user_info(message)
    _, patted = get_user_info(message.reply_to_message)
    await message.answer(
        f"{html.quote(patter)} погладил(а) {html.quote(patted)} по голове! 🥰",
        parse_mode="HTML",
    )


@router.message(F.text.lower().regexp(r"раф|гаф|гав"))
async def goodboy(message: types.Message) -> None:
    await message.reply(RNG.choice(("good boy!", "хороший песик!")))


@router.message(F.text.lower().contains("картошка"))
async def potato_reaction(message: types.Message) -> None:
    await message.reply("Кто-то сказал КАРТОШКА? 🥔")
