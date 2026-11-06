import aiohttp
import pytest

from chatbot.main import ConfigurableAiohttpSession


def test_tls_verification_is_enabled_by_default():
    session = ConfigurableAiohttpSession()

    assert session._connector_init["ssl"] is not False


def test_tls_verification_can_be_disabled_for_bot_session_only():
    original_connector_init = aiohttp.TCPConnector.__init__

    session = ConfigurableAiohttpSession(verify_tls=False)

    assert session._connector_init["ssl"] is False
    assert aiohttp.TCPConnector.__init__ is original_connector_init


@pytest.mark.asyncio
async def test_tls_setting_works_with_vpn_proxy_session():
    session = ConfigurableAiohttpSession(
        proxy="http://127.0.0.1:8080",
        verify_tls=False,
    )

    client_session = await session.create_session()

    assert client_session.connector._ssl is False
    await session.close()
