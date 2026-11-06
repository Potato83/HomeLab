from __future__ import annotations

import asyncio
import logging

from aiogram import F, Router, html, types
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .. import config
from .helpers import edit_text_if_changed, game_callback_payload, parse_bet, real_user_id
from ..services.admin_ops import get_pvp_setting
from ..services.economy import (
    BalanceLimitExceeded,
    InsufficientFunds,
    InvalidAmount,
    transfer,
)
from ..services.games import (
    GameAlreadyFinished,
    GameExpired,
    GameNotFound,
    InvalidPick,
    MoveAlreadyMade,
    NotParticipant,
    WrongParticipant,
    accept_pvp,
    accept_rps,
    create_challenge,
    decline_challenge,
    make_rps_move,
    play_instant_pvp,
)
from ..services.profiles import get_names

logger = logging.getLogger(__name__)
router = Router()
RPS_EMOJI = {"rock": "🪨", "paper": "📜", "scissors": "✂️"}


async def _challenge_error(
    callback: types.CallbackQuery,
    exc: Exception,
) -> None:
    if isinstance(exc, (WrongParticipant, NotParticipant)):
        text = "Это не твой вызов!"
    elif isinstance(exc, GameExpired):
        text = "Время игры истекло; возврат ставок обрабатывается автоматически."
    elif isinstance(exc, InsufficientFunds):
        text = "Не хватает картошки для ставки."
    elif isinstance(exc, MoveAlreadyMade):
        text = "Твой ход уже принят."
    elif isinstance(exc, BalanceLimitExceeded):
        text = (
            "Для зачисления нет места до лимита с учётом незавершённых выплат. "
            "Завершите игру и повторите действие."
        )
    elif isinstance(exc, GameAlreadyFinished):
        text = "Игра уже завершена."
    elif isinstance(exc, GameNotFound):
        text = "Игра не найдена. Возможно, это кнопка старой игры."
    elif isinstance(exc, (ValueError, InvalidPick)):
        text = "Некорректная игровая кнопка."
    else:
        text = "Не удалось обработать игровую кнопку."
    logger.info("game_callback_rejected reason=%s", type(exc).__name__)
    await callback.answer(text, show_alert=True)


@router.callback_query(F.data.startswith("pvp:a:"))
async def pvp_accept(callback: types.CallbackQuery) -> None:
    try:
        game_id = game_callback_payload(callback.data, "pvp:a:")
        result = await asyncio.to_thread(
            accept_pvp,
            game_id,
            callback.message.chat.id,
            callback.from_user.id,
        )
    except (
        ValueError,
        GameAlreadyFinished,
        GameExpired,
        GameNotFound,
        BalanceLimitExceeded,
        InsufficientFunds,
        WrongParticipant,
    ) as exc:
        await _challenge_error(callback, exc)
        return

    names = await asyncio.to_thread(
        get_names,
        callback.message.chat.id,
        (result.winner_id, result.loser_id),
    )
    await edit_text_if_changed(
        callback.message,
        f"⚔️ <b>Дуэль на {result.bet} 🥔 состоялась!</b>\n\n"
        f"🏆 Победитель: <b>{html.quote(names[result.winner_id])}</b> "
        f"(+{result.bet})\n"
        f"😭 Проигравший: <b>{html.quote(names[result.loser_id])}</b> "
        f"(-{result.bet})",
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("game:d:"))
async def challenge_decline(callback: types.CallbackQuery) -> None:
    try:
        game_id = game_callback_payload(callback.data, "game:d:")
        await asyncio.to_thread(
            decline_challenge,
            game_id,
            callback.message.chat.id,
            callback.from_user.id,
        )
    except (
        ValueError,
        GameAlreadyFinished,
        GameExpired,
        GameNotFound,
        BalanceLimitExceeded,
        WrongParticipant,
    ) as exc:
        await _challenge_error(callback, exc)
        return
    await edit_text_if_changed(
        callback.message,
        f"{html.quote(callback.from_user.full_name)} отклонил вызов. "
        "Ставка автора возвращена.",
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("rps:a:"))
async def rps_accepted(callback: types.CallbackQuery) -> None:
    try:
        game_id = game_callback_payload(callback.data, "rps:a:")
        await asyncio.to_thread(
            accept_rps,
            game_id,
            callback.message.chat.id,
            callback.from_user.id,
        )
    except (
        ValueError,
        GameAlreadyFinished,
        GameExpired,
        GameNotFound,
        BalanceLimitExceeded,
        InsufficientFunds,
        WrongParticipant,
    ) as exc:
        await _challenge_error(callback, exc)
        return

    keyboard = InlineKeyboardBuilder()
    for move, emoji in RPS_EMOJI.items():
        keyboard.button(
            text=emoji,
            callback_data=f"rps:m:{move}:{game_id}",
        )
    await edit_text_if_changed(
        callback.message,
        "🎮 <b>Игра началась!</b>\n"
        "Оба игрока, выберите жест на кнопках ниже 👇",
        reply_markup=keyboard.as_markup(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("rps:m:"))
async def rps_move(callback: types.CallbackQuery) -> None:
    try:
        move, game_id = game_callback_payload(callback.data, "rps:m:").split(":", 1)
        if not game_id:
            raise ValueError("empty RPS game ID")
        result = await asyncio.to_thread(
            make_rps_move,
            game_id,
            callback.message.chat.id,
            callback.from_user.id,
            move,
        )
    except (
        ValueError,
        GameAlreadyFinished,
        GameExpired,
        GameNotFound,
        BalanceLimitExceeded,
        InvalidPick,
        MoveAlreadyMade,
        NotParticipant,
    ) as exc:
        await _challenge_error(callback, exc)
        return

    if result.status == "waiting":
        await callback.answer("Твой ход принят!")
        return

    challenger_emoji = RPS_EMOJI[result.challenger_move]
    target_emoji = RPS_EMOJI[result.target_move]
    if result.winner_id is None:
        verdict = "🤝 <b>Ничья!</b> Обе ставки возвращены."
    else:
        names = await asyncio.to_thread(
            get_names,
            callback.message.chat.id,
            (result.winner_id,),
        )
        verdict = (
            f"🏆 Победил <b>{html.quote(names[result.winner_id])}</b>! "
            f"Приз: {result.bet * 2} 🥔."
        )
    await edit_text_if_changed(
        callback.message,
        "🏁 <b>Результат игры:</b>\n\n"
        f"{challenger_emoji} против {target_emoji}\n{verdict}",
        parse_mode="HTML",
    )
    await callback.answer("Ход принят!")


@router.message(Command("give"))
async def give_potato(message: types.Message) -> None:
    sender_id = real_user_id(message)
    if sender_id is None:
        await message.answer("Передавать картошку могут только пользователи.")
        return
    if not message.reply_to_message:
        await message.answer(
            "Ответь на сообщение того, кому хочешь дать картошку!"
        )
        return
    receiver_id = real_user_id(message.reply_to_message)
    if receiver_id is None:
        await message.answer("Этому отправителю нельзя передать картошку.")
        return
    args = message.text.split() if message.text else []
    try:
        amount = int(args[1])
    except (IndexError, ValueError):
        await message.answer("Укажи сумму! Пример: /give 10")
        return
    if amount <= 0 or amount > config.MAX_BALANCE:
        await message.answer("Сумма должна быть положительной и разумной.")
        return
    if sender_id == receiver_id:
        await message.answer("Передавать картошку самому себе нельзя! 🥔")
        return
    try:
        await asyncio.to_thread(
            transfer,
            message.chat.id,
            sender_id,
            receiver_id,
            amount,
            operation_key=f"give:{message.chat.id}:{message.message_id}",
        )
    except InsufficientFunds:
        await message.answer("У тебя недостаточно картошки!")
        return
    except BalanceLimitExceeded:
        await message.answer(
            "Перевод отменён: зачисление превысит лимит с учётом "
            "незавершённых выплат получателя."
        )
        return
    except InvalidAmount:
        await message.answer("Некорректная сумма.")
        return
    await message.answer(
        f"✅ Ты передал {amount} 🥔 пользователю "
        f"{html.quote(message.reply_to_message.from_user.full_name)}!",
        parse_mode="HTML",
    )


@router.message(Command("pvp"))
async def pvp_battle(message: types.Message) -> None:
    challenger_id = real_user_id(message)
    if challenger_id is None:
        await message.answer("Дуэли доступны только обычным пользователям.")
        return
    if not message.reply_to_message:
        await message.answer(
            "Ответь на сообщение соперника. Пример: /pvp 5"
        )
        return
    target_id = real_user_id(message.reply_to_message)
    bet = parse_bet(message.text)
    if target_id is None:
        await message.answer("С этой целью нельзя играть на картошку.")
        return
    if target_id == challenger_id:
        await message.answer("Нельзя драться с самим собой 🤡")
        return
    if not bet:
        await message.answer(
            f"Ставка должна быть от {config.MIN_BET} до {config.MAX_BET}."
        )
        return

    confirmation_enabled = await asyncio.to_thread(
        get_pvp_setting,
        message.chat.id,
    )
    try:
        if not confirmation_enabled:
            result = await asyncio.to_thread(
                play_instant_pvp,
                message.chat.id,
                challenger_id,
                target_id,
                bet,
                operation_key=f"pvp:{message.chat.id}:{message.message_id}",
            )
            names = await asyncio.to_thread(
                get_names,
                message.chat.id,
                (result.winner_id, result.loser_id),
            )
            await message.answer(
                "⚔️ <b>Мгновенный бой!</b>\n"
                f"🏆 Победитель: "
                f"{html.quote(names[result.winner_id])} (+{bet})\n"
                f"😭 Проигравший: "
                f"{html.quote(names[result.loser_id])} (-{bet})",
                parse_mode="HTML",
            )
            return
        challenge = await asyncio.to_thread(
            create_challenge,
            "pvp",
            message.chat.id,
            challenger_id,
            target_id,
            bet,
            operation_key=f"pvp:{message.chat.id}:{message.message_id}",
        )
    except InsufficientFunds as exc:
        await message.answer(
            f"Не хватает картошки: доступно {exc.balance}, нужно {exc.required}."
        )
        return
    except BalanceLimitExceeded:
        await message.answer(
            "Лимит баланса не позволяет провести эту дуэль."
        )
        return
    if challenge.status != "pending":
        await message.answer("Этот вызов уже обработан.")
        return

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text="Принять ✅",
                    callback_data=f"pvp:a:{challenge.game_id}",
                ),
                types.InlineKeyboardButton(
                    text="Отклонить ❌",
                    callback_data=f"game:d:{challenge.game_id}",
                ),
            ]
        ]
    )
    await message.answer(
        f"⚔️ Игрок {html.quote(message.reply_to_message.from_user.full_name)}, "
        f"тебя вызывает {html.quote(message.from_user.full_name)}!\n"
        f"Ставка {bet} 🥔 уже зарезервирована у автора.",
        reply_markup=keyboard,
        parse_mode="HTML",
    )


@router.message(Command("rps"))
async def rps_challenge(message: types.Message) -> None:
    challenger_id = real_user_id(message)
    if challenger_id is None:
        await message.answer("Игра доступна только обычным пользователям.")
        return
    if not message.reply_to_message:
        await message.answer("Ответь на сообщение противника!")
        return
    target_id = real_user_id(message.reply_to_message)
    bet = parse_bet(message.text)
    if target_id is None:
        await message.answer("С этой целью нельзя играть на картошку.")
        return
    if target_id == challenger_id:
        await message.answer("Нельзя вызвать самого себя.")
        return
    if not bet:
        await message.answer(
            f"Ставка должна быть от {config.MIN_BET} до {config.MAX_BET}."
        )
        return
    try:
        challenge = await asyncio.to_thread(
            create_challenge,
            "rps",
            message.chat.id,
            challenger_id,
            target_id,
            bet,
            operation_key=f"rps:{message.chat.id}:{message.message_id}",
        )
    except InsufficientFunds as exc:
        await message.answer(
            f"Не хватает картошки: доступно {exc.balance}, нужно {exc.required}."
        )
        return
    except BalanceLimitExceeded:
        await message.answer(
            "Лимит баланса не позволяет провести эту игру."
        )
        return
    if challenge.status != "pending":
        await message.answer("Этот вызов уже обработан.")
        return

    keyboard = InlineKeyboardBuilder()
    keyboard.button(
        text="Принять вызов ✅",
        callback_data=f"rps:a:{challenge.game_id}",
    )
    keyboard.button(
        text="Отказаться ❌",
        callback_data=f"game:d:{challenge.game_id}",
    )
    await message.answer(
        f"🤜 <b>{html.quote(message.from_user.full_name)}</b> вызывает "
        f"<b>{html.quote(message.reply_to_message.from_user.full_name)}</b> "
        f"на КНБ!\nСтавка {bet} 🥔 зарезервирована у автора.",
        reply_markup=keyboard.as_markup(),
        parse_mode="HTML",
    )
