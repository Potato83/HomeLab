"""Compact control cards. All mutations require a server-side confirmation."""
import logging
from html import escape

from aiogram import Router, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from serverbot.control import ControlClient, ControlError, ICONS, LABELS, STATES, Target
from serverbot.handlers import _edit_status, format_report_messages

router = Router(name='control')
logger = logging.getLogger('serverbot.control')
ACTIONS = {'start': 'Запустить', 'stop': 'Остановить', 'restart': 'Перезапустить'}


def button(text, data):
    return InlineKeyboardButton(text=text, callback_data='control:' + data)


def keyboard(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


def action_label(target, action):
    if target == 'kali':
        return {'start': 'Включить', 'stop': 'Выключить', 'restart': 'Перезагрузить'}[action]
    return ACTIONS[action]


def allowed_actions(row):
    return ('stop', 'restart') if row.state == 'running' else ('start',) if row.state == 'stopped' else ()


def card(row: Target, notice=''):
    state = 'Остановлен вручную' if row.manual else STATES[row.state]
    text = f'{ICONS[row.state]} <b>{LABELS[row.target]}</b>\n{state}'
    if row.target == 'kali':
        text += '\n<i>Состояние VM по libvirt; загрузка ОС и SSH могут занять время.</i>'
    if notice:
        text += '\n\n' + escape(notice)
    rows = [[button(action_label(row.target, action), f'ask:{row.target}:{action}')
             for action in allowed_actions(row)]] if allowed_actions(row) else []
    if row.target != 'kali':
        rows.append([button('Ошибки', 'errors:' + row.target)])
    rows.append([button('↻ Обновить', 'card:' + row.target), button('← Назад', 'home')])
    return text, keyboard(rows)


async def show_home(message, control, edit=True):
    targets = await control.targets()
    text = '<b>⚙️ Управление</b>\nВыбери объект.'
    rows = [[button(f'{ICONS[row.state]} {LABELS[row.target]}', 'card:' + row.target)] for row in targets]
    rows.append([InlineKeyboardButton(text='← Сводка', callback_data='status:summary')])
    markup = keyboard(rows)
    if edit:
        await _edit_status(message, text, markup)
    else:
        await message.answer(text, parse_mode='HTML', reply_markup=markup)


@router.message(Command('panel'))
async def panel_command(message: types.Message, control: ControlClient):
    if not control.allow_read(message.from_user.id):
        return
    try:
        await show_home(message, control, edit=False)
    except ControlError as error:
        await message.answer(str(error))


async def is_control_callback(callback: types.CallbackQuery) -> bool:
    return (callback.data or '').startswith('control:')


@router.callback_query(is_control_callback)
async def panel_callback(callback: types.CallbackQuery, control: ControlClient):
    message = callback.message
    if not isinstance(message, types.Message):
        await callback.answer()
        return
    parts = (callback.data or '').split(':')
    # Acknowledge before calling the system helper (a graceful VM shutdown takes time).
    await callback.answer()
    try:
        if len(parts) == 3 and parts[1] == 'do':
            item = control.consume(parts[2], callback.from_user.id, message.chat.id, message.message_id)
            logger.info('action_requested user_id=%s target=%s action=%s', callback.from_user.id, item.target, item.action)
            await _edit_status(message, f'<b>{LABELS[item.target]}</b>\nВыполняю: {action_label(item.target, item.action).lower()}…', None)
            try:
                row = await control.execute(item.target, item.action)
            except ControlError:
                logger.warning('action_failed user_id=%s target=%s action=%s', callback.from_user.id, item.target, item.action)
                raise
            logger.info('action_completed user_id=%s target=%s action=%s', callback.from_user.id, item.target, item.action)
            text, markup = card(row, 'Готово. Состояние проверено.')
            await _edit_status(message, text, markup)
            return
        if not control.allow_read(callback.from_user.id):
            return
        control.forget_message(message.chat.id, message.message_id)
        if parts == ['control', 'home']:
            await show_home(message, control)
        elif len(parts) == 3 and parts[1] == 'card' and parts[2] in LABELS:
            text, markup = card(await control.status(parts[2]))
            await _edit_status(message, text, markup)
        elif len(parts) == 4 and parts[1] == 'ask' and parts[2] in LABELS and parts[3] in ACTIONS:
            target, action = parts[2:]
            row = await control.status(target)
            if action not in allowed_actions(row):
                raise ControlError('Состояние изменилось. Обнови карточку.')
            token = control.confirm(callback.from_user.id, message.chat.id, message.message_id, target, action)
            text = f'<b>{action_label(target, action)} {LABELS[target]}?</b>'
            if target == 'kali' and action in {'stop', 'restart'}:
                text += '\nРаботающие внутри VM задачи будут прерваны.'
            await _edit_status(message, text, keyboard([
                [button('Подтвердить', 'do:' + token)],
                [button('Отмена', 'card:' + target)],
            ]))
        elif len(parts) == 3 and parts[1] == 'errors' and parts[2] in LABELS and parts[2] != 'kali':
            target = parts[2]
            errors = await control.errors(target)
            # Tracebacks remain whole within the helper limit; Telegram chunks are safely escaped.
            # Earlier chunks are sent separately; the last replaces the card and keeps navigation.
            chunks = format_report_messages(errors) if errors else ['Последних ошибок нет.']
            for chunk in chunks[:-1]:
                await message.answer(chunk, parse_mode='HTML')
            await _edit_status(message, f'<b>{LABELS[target]} · ошибки</b>\n'
                               '<i>Последние предупреждения и ошибки из доступного хвоста журналов.</i>\n\n' + chunks[-1],
                               keyboard([[button('↻ Обновить', 'errors:' + target), button('← Назад', 'card:' + target)]]))
        else:
            raise ControlError('Действие недоступно.')
    except ControlError as error:
        await _edit_status(message, escape(str(error)), keyboard([[button('← Управление', 'home')]]))
