from __future__ import annotations

import asyncio
from datetime import datetime

from aiogram import F, Router, html, types
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .. import config
from .helpers import MOSCOW_TIMEZONE, edit_text_if_changed, real_user_id
from ..services.admin_ops import (
    SETTING_VALUES,
    AdminOperationError,
    InvalidConfirmation,
    create_reset_confirmation,
    get_chat_summary,
    list_chats,
    recent_admin_actions,
    reset_chat,
    set_setting,
    toggle_pvp,
)
from ..services.economy import (
    BalanceLimitExceeded,
    InvalidAmount,
    admin_adjust,
)
from ..services.loans import LOAN_NOTIFICATION_LOCK
from ..services.moderation import SLEEP_ACTION_LOCK

router = Router()
ADMIN_CHAT_PAGE_SIZE = 40
ADMIN_AUDIT_LIMIT = 15


async def _require_admin(callback: types.CallbackQuery) -> bool:
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Нет доступа.", show_alert=True)
        return False
    return True


def _format_number(value: int) -> str:
    return f"{value:,}".replace(",", " ")


async def _render_admin_home(
    target: types.Message,
    *,
    edit: bool,
) -> None:
    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text="💬 Чаты",
                    callback_data="adm:chats",
                ),
                types.InlineKeyboardButton(
                    text="📋 Журнал",
                    callback_data="adm:journal",
                ),
            ]
        ]
    )
    text = "🥔 <b>Панель управления</b>\nВыберите раздел:"
    if edit:
        await edit_text_if_changed(
            target,
            text,
            reply_markup=keyboard,
            parse_mode="HTML",
        )
    else:
        await target.answer(text, reply_markup=keyboard, parse_mode="HTML")


async def _render_admin_chats(
    target: types.Message,
    *,
    edit: bool,
    page: int = 0,
) -> None:
    chats = await asyncio.to_thread(list_chats)
    if not chats:
        text = "В базе пока нет данных о группах."
        keyboard = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text="⬅️ В меню",
                        callback_data="adm:home",
                    )
                ]
            ]
        )
        if edit:
            await edit_text_if_changed(target, text, reply_markup=keyboard)
        else:
            await target.answer(text, reply_markup=keyboard)
        return
    page_count = (len(chats) + ADMIN_CHAT_PAGE_SIZE - 1) // ADMIN_CHAT_PAGE_SIZE
    page = max(0, min(page, page_count - 1))
    builder = InlineKeyboardBuilder()
    first_chat = page * ADMIN_CHAT_PAGE_SIZE
    for chat_id, title in chats[first_chat : first_chat + ADMIN_CHAT_PAGE_SIZE]:
        display_name = title or f"ID: {chat_id}"
        button_text = f"🏗 {display_name}"
        if len(button_text) > 60:
            button_text = button_text[:59] + "…"
        builder.button(
            text=button_text,
            callback_data=f"adm:chat:{chat_id}",
        )
    builder.adjust(1)
    if page_count > 1:
        navigation = []
        if page > 0:
            navigation.append(
                types.InlineKeyboardButton(
                    text="⬅️",
                    callback_data=f"adm:page:{page - 1}",
                )
            )
        if page < page_count - 1:
            navigation.append(
                types.InlineKeyboardButton(
                    text="➡️",
                    callback_data=f"adm:page:{page + 1}",
                )
            )
        builder.row(*navigation)
    builder.row(
        types.InlineKeyboardButton(
            text="⬅️ В меню",
            callback_data="adm:home",
        )
    )
    text = (
        "💬 <b>Чаты</b>\n"
        f"Страница {page + 1}/{page_count}. Выберите чат:"
    )
    if edit:
        await edit_text_if_changed(
            target,
            text,
            reply_markup=builder.as_markup(),
            parse_mode="HTML",
        )
    else:
        await target.answer(
            text,
            reply_markup=builder.as_markup(),
            parse_mode="HTML",
        )


async def _render_chat_settings(
    callback: types.CallbackQuery,
    chat_id: int,
) -> None:
    summary = await asyncio.to_thread(get_chat_summary, chat_id)
    if summary is None:
        await edit_text_if_changed(
            callback.message,
            "Чат больше не найден в базе.",
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text="⬅️ К чатам",
                            callback_data="adm:chats",
                        )
                    ]
                ]
            ),
        )
        return

    chat_title = html.quote(summary.title or f"Чат {chat_id}")
    pvp_mode = "с подтверждением" if summary.pvp_confirm else "мгновенно"
    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text="🥔 Кулдаун /dig",
                    callback_data=f"adm:set:{chat_id}:dig_cd",
                ),
                types.InlineKeyboardButton(
                    text="💰 Цена /sleep",
                    callback_data=f"adm:set:{chat_id}:sleep_price",
                ),
            ],
            [
                types.InlineKeyboardButton(
                    text="⏳ Длительность /sleep",
                    callback_data=f"adm:set:{chat_id}:sleep_duration",
                ),
                types.InlineKeyboardButton(
                    text=f"⚔️ PvP: {pvp_mode}",
                    callback_data=f"adm:pvp:{chat_id}",
                ),
            ],
            [
                types.InlineKeyboardButton(
                    text="🧹 Сбросить чат",
                    callback_data=f"adm:reset:{chat_id}",
                )
            ],
            [
                types.InlineKeyboardButton(
                    text="⬅️ К чатам",
                    callback_data="adm:chats",
                )
            ],
        ]
    )
    text = (
        f"💬 <b>{chat_title}</b> <code>{chat_id}</code>\n"
        f"👥 Известно участников: {summary.known_users}\n"
        f"🥔 Балансы: {_format_number(summary.total_balance)}"
        f" · Долги: {_format_number(summary.total_debt)} 🥔\n"
        f"🎮 Игр: {summary.active_games}"
        f" · 🏦 Займов: {summary.active_loans}"
        f" · 💤 Спят: {summary.sleeping_users}\n\n"
        f"⚙️ /dig: {summary.dig_cd} ч"
        f" · /sleep: {summary.sleep_price} 🥔 / {summary.sleep_duration} мин"
        f" · PvP {pvp_mode}"
    )
    await edit_text_if_changed(
        callback.message,
        text,
        reply_markup=keyboard,
        parse_mode="HTML",
    )


@router.callback_query(F.data == "adm:home")
@router.callback_query(F.data == "adm:back")
async def admin_back_to_home(callback: types.CallbackQuery) -> None:
    if not await _require_admin(callback):
        return
    await _render_admin_home(callback.message, edit=True)
    await callback.answer()


@router.callback_query(F.data == "adm:chats")
async def admin_show_chats(callback: types.CallbackQuery) -> None:
    if not await _require_admin(callback):
        return
    await _render_admin_chats(callback.message, edit=True)
    await callback.answer()


@router.callback_query(F.data == "adm:journal")
async def admin_show_journal(callback: types.CallbackQuery) -> None:
    if not await _require_admin(callback):
        return
    entries = await asyncio.to_thread(
        recent_admin_actions,
        limit=ADMIN_AUDIT_LIMIT,
    )
    action_names = {
        "adjust_balance": "Корректировка баланса",
        "set_setting": "Изменение настройки",
        "toggle_pvp": "Режим PvP",
        "reset_chat": "Сброс чата",
    }
    lines = ["📋 <b>Последние действия владельца</b>"]
    for entry in entries:
        timestamp = datetime.fromtimestamp(
            entry.created_at,
            MOSCOW_TIMEZONE,
        ).strftime("%d.%m.%Y %H:%M")
        action = html.quote(
            action_names.get(entry.action, entry.action)
        )
        chat = (
            f"чат <code>{entry.chat_id}</code>"
            if entry.chat_id is not None
            else "без чата"
        )
        details = html.quote(entry.details or "")
        lines.append(
            f"\n<code>{timestamp}</code> · {action} · {chat}\n{details}"
        )
    if not entries:
        lines.append("\nЗаписей пока нет.")
    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text="⬅️ В меню",
                    callback_data="adm:home",
                )
            ]
        ]
    )
    await edit_text_if_changed(
        callback.message,
        "\n".join(lines),
        reply_markup=keyboard,
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:page:"))
async def admin_chat_page(callback: types.CallbackQuery) -> None:
    if not await _require_admin(callback):
        return
    try:
        page = int(callback.data.rsplit(":", 1)[-1])
    except ValueError:
        await callback.answer("Некорректная страница.", show_alert=True)
        return
    await _render_admin_chats(callback.message, edit=True, page=page)
    await callback.answer()


@router.callback_query(F.data.startswith("adm:chat:"))
async def admin_chat_settings(callback: types.CallbackQuery) -> None:
    if not await _require_admin(callback):
        return
    try:
        chat_id = int(callback.data.rsplit(":", 1)[-1])
    except ValueError:
        await callback.answer("Некорректный чат.", show_alert=True)
        return
    await _render_chat_settings(callback, chat_id)
    await callback.answer()


@router.callback_query(F.data.startswith("adm:pvp:"))
async def toggle_pvp_confirm(callback: types.CallbackQuery) -> None:
    if not await _require_admin(callback):
        return
    try:
        chat_id = int(callback.data.rsplit(":", 1)[-1])
    except ValueError:
        await callback.answer("Некорректный чат.", show_alert=True)
        return
    value = await asyncio.to_thread(
        toggle_pvp,
        chat_id,
        admin_id=callback.from_user.id,
    )
    await _render_chat_settings(callback, chat_id)
    status = "включено" if value else "выключено"
    await callback.answer(
        f"Подтверждение PvP {status}.",
        show_alert=True,
    )


@router.callback_query(F.data.startswith("adm:reset:"))
async def clear_db_ask(callback: types.CallbackQuery) -> None:
    if not await _require_admin(callback):
        return
    try:
        chat_id = int(callback.data.rsplit(":", 1)[-1])
    except ValueError:
        await callback.answer("Некорректный чат.", show_alert=True)
        return
    token = await asyncio.to_thread(
        create_reset_confirmation,
        chat_id,
        callback.from_user.id,
    )
    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text="ДА, СБРОСИТЬ ЧАТ",
                    callback_data=f"adm:confirm:{token}",
                )
            ],
            [
                types.InlineKeyboardButton(
                    text="Отмена",
                    callback_data=f"adm:chat:{chat_id}",
                )
            ],
        ]
    )
    await edit_text_if_changed(
        callback.message,
        "⚠️ Безвозвратно удалятся пользователи, "
        f"деньги, предметы, займы и игры чата <code>{chat_id}</code>.\n"
        "Резервная копия не создаётся. Подтверждение действует 2 минуты.",
        reply_markup=keyboard,
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:confirm:"))
async def clear_db_execute(callback: types.CallbackQuery) -> None:
    if not await _require_admin(callback):
        return
    token = callback.data.split(":", 2)[-1]
    try:
        async with SLEEP_ACTION_LOCK, LOAN_NOTIFICATION_LOCK:
            result = await asyncio.to_thread(
                reset_chat,
                token,
                callback.from_user.id,
            )
    except InvalidConfirmation:
        await callback.answer(
            "Подтверждение истекло или уже использовано.",
            show_alert=True,
        )
        return
    await edit_text_if_changed(
        callback.message,
        f"✅ Данные чата <code>{result.chat_id}</code> удалены.",
        parse_mode="HTML",
    )
    await callback.answer("Данные чата удалены.", show_alert=True)


@router.callback_query(F.data.startswith("adm:set:"))
async def set_value_menu(callback: types.CallbackQuery) -> None:
    if not await _require_admin(callback):
        return
    try:
        _, _, chat_id_raw, parameter = callback.data.split(":")
        chat_id = int(chat_id_raw)
        values = sorted(SETTING_VALUES[parameter])
    except (ValueError, KeyError):
        await callback.answer("Некорректный параметр.", show_alert=True)
        return
    labels = {
        "dig_cd": "кулдаун /dig в часах",
        "sleep_price": "цену /sleep в 🥔",
        "sleep_duration": "длительность /sleep в минутах",
    }
    builder = InlineKeyboardBuilder()
    for value in values:
        builder.button(
            text=str(value),
            callback_data=f"adm:save:{chat_id}:{parameter}:{value}",
        )
    builder.button(
        text="⬅️ Назад",
        callback_data=f"adm:chat:{chat_id}",
    )
    builder.adjust(3)
    await edit_text_if_changed(
        callback.message,
        f"Выберите {labels[parameter]} для чата {chat_id}:",
        reply_markup=builder.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:save:"))
async def save_value(callback: types.CallbackQuery) -> None:
    if not await _require_admin(callback):
        return
    try:
        _, _, chat_id_raw, parameter, value_raw = callback.data.split(":")
        chat_id = int(chat_id_raw)
        value = int(value_raw)
        await asyncio.to_thread(
            set_setting,
            chat_id,
            parameter,
            value,
            admin_id=callback.from_user.id,
        )
    except (ValueError, AdminOperationError):
        await callback.answer("Недопустимое значение.", show_alert=True)
        return
    await _render_chat_settings(callback, chat_id)
    await callback.answer("Настройка сохранена.", show_alert=True)


@router.message(Command("admin"), F.chat.type == "private")
async def admin_main(message: types.Message) -> None:
    if not config.ADMIN_IDS:
        await message.answer("Ошибка: ADMIN_IDS не настроен в .env")
        return
    if not message.from_user or message.from_user.id not in config.ADMIN_IDS:
        return
    await _render_admin_home(message, edit=False)


@router.message(Command("add"), F.reply_to_message)
async def admin_add_potato(message: types.Message) -> None:
    if (
        not config.ADMIN_IDS
        or not message.from_user
        or message.from_user.id not in config.ADMIN_IDS
    ):
        return
    target_id = real_user_id(message.reply_to_message)
    if target_id is None:
        await message.answer("Цель должна быть обычным пользователем.")
        return
    args = message.text.split() if message.text else []
    try:
        amount = int(args[1])
    except (IndexError, ValueError):
        await message.answer("Укажи сумму! Пример: /add 1000")
        return
    if amount == 0 or abs(amount) > config.MAX_BALANCE:
        await message.answer("Сумма вне допустимого диапазона.")
        return
    try:
        balance = await asyncio.to_thread(
            admin_adjust,
            message.chat.id,
            target_id,
            amount,
            admin_id=message.from_user.id,
            operation_key=f"admin:{message.chat.id}:{message.message_id}",
        )
    except (InvalidAmount, BalanceLimitExceeded):
        await message.answer("Изменение превысило допустимый баланс.")
        return
    action = "выдал" if amount > 0 else "забрал"
    await message.answer(
        f"👑 Админ {action} {abs(amount)} 🥔 у "
        f"{html.quote(message.reply_to_message.from_user.full_name)}. "
        f"Баланс: {balance} 🥔.",
        parse_mode="HTML",
    )
