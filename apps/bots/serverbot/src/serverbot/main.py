import argparse
import asyncio
import logging
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand
from aiohttp import ClientSession, ClientTimeout, DummyCookieJar
from dotenv import load_dotenv

from serverbot.config import load_config, load_env_config
from serverbot.handlers import router
from serverbot.logging_setup import setup_logging
from serverbot.middlewares import AccessMiddleware
from serverbot.scheduler import create_scheduler
from serverbot.state import StateStore

logger = logging.getLogger(__name__)


BOT_COMMANDS = [
    BotCommand(command="start", description="Показать команды"),
    BotCommand(command="status", description="Сводка по серверу"),
    BotCommand(command="logs", description="Прочитать разрешённый журнал"),
]


async def main() -> None:
    # Read secrets and user IDs from .env, monitoring checks from config.toml
    load_dotenv(Path.cwd() / ".env")
    setup_logging()
    settings = load_env_config()
    config = load_config()
    state = StateStore(config.state_file)
    # Check that state can be saved before sending any Telegram alerts
    state.save(state.states)
    dp = Dispatcher()
    # Check access before any command handler runs
    dp.update.outer_middleware(AccessMiddleware(settings.allowed_user_ids))
    dp.include_router(router)
    session = AiohttpSession(proxy=settings.proxy, timeout=30)
    # Telegram uses the optional proxy; local HTTP checks connect directly
    # Both sessions are closed automatically when these contexts end
    async with (
        Bot(token=settings.token, session=session) as bot,
        ClientSession(timeout=ClientTimeout(total=5), cookie_jar=DummyCookieJar()) as http_session,):
        try:
            await bot.set_my_commands(BOT_COMMANDS)
        except TelegramAPIError as exc:
            # A command menu is helpful but must not prevent polling from starting.
            logger.warning(
                "Telegram command menu was not updated (%s)", type(exc).__name__
            )
        scheduler = create_scheduler(bot, config, http_session, settings.report_user_ids,
                                     state=state, alert_user_ids=settings.allowed_user_ids)
        scheduler.start()
        try:
            # Aiogram passes config and http_session to handlers by argument name
            await dp.start_polling(bot, config=config, http_session=http_session, close_bot_session=False)
        finally:
            # Stop scheduled jobs before closing the network sessions
            scheduler.shutdown(wait=False)
            # Give APScheduler's shutdown callback a turn in the event loop
            await asyncio.sleep(0)


def cli() -> None:
    argparse.ArgumentParser(description="serverbot").parse_args()
    asyncio.run(main())


if __name__ == "__main__":
    cli()
