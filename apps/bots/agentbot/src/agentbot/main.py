import argparse
import asyncio
import contextlib
import fcntl
import logging
import os
import sys

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramNetworkError, TelegramBadRequest, TelegramForbiddenError
from aiogram.types import LinkPreviewOptions, BotCommand, BotCommandScopeChat, MenuButtonCommands
from dotenv import load_dotenv

from .access import owner_allowed
from .backend import OpenCode
from .config import load
from .service import LABELS, TERMINAL, Service
from .store import Store



class OwnerOnly(BaseMiddleware):
    def __init__(self, owner):
        self.owner = owner

    async def __call__(self, handler, event, data):
        user, chat = data.get('event_from_user'), data.get('event_chat')
        if not owner_allowed(self.owner, user.id if user else None,
                             chat.id if chat else None, chat.type if chat else None):
            return None
        return await handler(event, data)


def dispatcher(store, backend, owner):
    from .handlers import install_handlers
    dp = Dispatcher()
    dp.update.outer_middleware(OwnerOnly(owner))
    install_handlers(dp, store, backend, owner)
    return dp


async def poll_reliably(dp, bot, setup=None):
    warned = False
    configured = False
    while True:
        try:
            if setup is not None and not configured:
                await setup()
                configured = True
            await dp.start_polling(bot,handle_as_tasks=False,allowed_updates=['message','callback_query'],close_bot_session=False)
            return
        except (TelegramNetworkError, asyncio.TimeoutError, OSError):
            if not warned:
                print('Telegram temporarily unavailable; retrying without dropping saved work.',file=sys.stderr)
                warned = True
            await asyncio.sleep(10)


async def main(settings):
    from . import ui
    store = Store(settings.state)
    backend = OpenCode(settings)
    try:
        session = AiohttpSession(proxy=settings.proxy or None, timeout=30)
        async with Bot(settings.token, session=session) as bot:
            async def send(text):
                await bot.send_message(settings.owner, text, parse_mode=None,
                                       link_preview_options=LinkPreviewOptions(is_disabled=True),reply_markup=ui.menu_keyboard())

            async def send_task(sid,text):
                row=store.session(sid);run=store.latest(sid)
                await bot.send_message(settings.owner,text,parse_mode=None,
                                       link_preview_options=LinkPreviewOptions(is_disabled=True),
                                       reply_markup=ui.card_keyboard(row,running=bool(run and run['state'] not in TERMINAL)))

            async def setup_commands():
                with contextlib.suppress(TelegramBadRequest, TelegramForbiddenError):
                    await bot.set_my_commands([
                        BotCommand(command=c, description=d) for c, d in [
                            ('new','Новая задача'),('board','CTF-борда'),('sessions','Мои задачи'),
                            ('status','Состояние'),('result','Последний ответ'),('stop','Остановить'),
                            ('cancel','Выйти из заполнения'),('help','Помощь')]],
                        scope=BotCommandScopeChat(chat_id=settings.owner))
                    await bot.set_chat_menu_button(chat_id=settings.owner, menu_button=MenuButtonCommands())
            service = Service(store, backend, send, settings.poll, send_task=send_task)
            task = asyncio.create_task(service.watch())
            try:
                # Complete the short handler (including disk commit) before acknowledging an update.
                await poll_reliably(dispatcher(store,backend,settings.owner),bot,setup_commands)
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
    finally:
        await backend.close()
        store.close()


def cli():
    parser = argparse.ArgumentParser(description='Owner-only Telegram client for an existing OpenCode API')
    parser.add_argument('--check', action='store_true', help='Validate configuration and state access without networking')
    args = parser.parse_args()
    os.umask(0o077)
    # Third-party exception logs can contain Telegram updates and authenticated URLs.
    logging.disable(logging.CRITICAL)
    load_dotenv('.env')
    try:
        settings = load()
        if os.geteuid() == 0:
            raise ValueError('Run AgentBot as an unprivileged user')
        settings.state.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with settings.state.with_suffix('.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if args.check:
                Store(settings.state).close()
                print('Configuration and state: OK; network not checked')
            else:
                asyncio.run(main(settings))
    except KeyboardInterrupt:
        pass
    except Exception as error:
        print(f'AgentBot could not start ({type(error).__name__}). Check local configuration and permissions.', file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == '__main__':
    cli()
