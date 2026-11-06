import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import ClientConnectionError, ClientSession, web

from serverbot.collectors.http import get_http_status
from serverbot.config import HttpCheckConfig


@pytest.mark.parametrize("status", [200, 503, 302])
def test_http_status_with_real_local_server(status):
    async def run():
        async def health(request):
            assert request.headers["X-API-Key"] == "test-secret"
            return web.Response(status=status, headers={"Location": "/redirect-target"} if status == 302 else {})

        app = web.Application()
        app.router.add_get("/health", health)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            check = HttpCheckConfig("Service", f"http://127.0.0.1:{port}/health", auth_env="SERVICE_KEY", auth_header="X-API-Key")
            async with ClientSession() as session:
                result = await get_http_status(check, session)
            assert result.ok == (status == 200)
            assert result.status == status
        finally:
            await runner.cleanup()

    import os
    from unittest.mock import patch
    with patch.dict(os.environ, SERVICE_KEY="test-secret"):
        asyncio.run(run())


def test_timeout_against_slow_server():
    async def run():
        async def health(request):
            await asyncio.sleep(0.1)
            return web.Response()

        app = web.Application()
        app.router.add_get("/health", health)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            async with ClientSession() as session:
                result = await get_http_status(HttpCheckConfig("Slow", f"http://127.0.0.1:{port}/health", timeout=0.01), session)
            assert not result.ok
            assert result.error == "Timeout"
        finally:
            await runner.cleanup()
    asyncio.run(run())


def test_network_error_does_not_expose_details():
    session = MagicMock()
    session.get.return_value.__aenter__ = AsyncMock(side_effect=ClientConnectionError("URL with secret-token"))
    result = asyncio.run(get_http_status(HttpCheckConfig("Service", "http://localhost/health"), session))
    assert result.error == "HTTP request failed"
    assert "secret-token" not in str(result)
    assert session.get.call_args.kwargs["timeout"].total == 5.0
    assert session.get.call_args.kwargs["allow_redirects"] is False


def test_missing_auth_does_not_make_request(monkeypatch):
    monkeypatch.delenv("MISSING_KEY", raising=False)
    session = MagicMock()
    check = HttpCheckConfig("Service", "http://localhost/health", auth_env="MISSING_KEY")
    result = asyncio.run(get_http_status(check, session))
    assert result.error == "Missing authentication setting"
    session.get.assert_not_called()
