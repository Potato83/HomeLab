import asyncio
from unittest.mock import AsyncMock,patch

from agentbot.main import poll_reliably


def test_polling_startup_recovers_from_transient_network_without_leaking_errors(capsys):
    async def run():
        dp=AsyncMock()
        dp.start_polling.side_effect=[TimeoutError('private-token'),None]
        with patch('agentbot.main.asyncio.sleep',new_callable=AsyncMock) as sleep:
            await poll_reliably(dp,object())
        assert dp.start_polling.await_count==2
        sleep.assert_awaited_once_with(10)
        assert dp.start_polling.call_args.kwargs['allowed_updates']==['message','callback_query']
    asyncio.run(run())
    text=capsys.readouterr().err
    assert 'retrying' in text and 'private-token' not in text


def test_orderly_polling_stop_is_not_restarted():
    async def run():
        dp=AsyncMock()
        with patch('agentbot.main.asyncio.sleep',new_callable=AsyncMock) as sleep:
            await poll_reliably(dp,object())
        dp.start_polling.assert_awaited_once()
        sleep.assert_not_awaited()
    asyncio.run(run())


def test_command_menu_registration_retries_after_connection_failure():
    async def run():
        dp=AsyncMock()
        setup=AsyncMock(side_effect=[TimeoutError('private-token'),None])
        with patch('agentbot.main.asyncio.sleep',new_callable=AsyncMock):
            await poll_reliably(dp,object(),setup)
        assert setup.await_count==2
        dp.start_polling.assert_awaited_once()
    asyncio.run(run())
