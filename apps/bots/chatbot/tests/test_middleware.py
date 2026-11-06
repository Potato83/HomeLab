from __future__ import annotations

from types import SimpleNamespace

import pytest

from potato_bot import middlewares
from potato_bot.middlewares import AntiSpamMiddleware


@pytest.mark.asyncio
async def test_media_without_text_passes_middleware(monkeypatch) -> None:
    monkeypatch.setattr(middlewares, "save_user_data", lambda *args: None)
    event = SimpleNamespace(
        text=None,
        sender_chat=None,
        chat=SimpleNamespace(id=1, title="Chat", type="private"),
        from_user=SimpleNamespace(
            id=2,
            is_bot=False,
            full_name="User",
        ),
    )
    called = False

    async def handler(received, data):
        nonlocal called
        called = received is event and data == {}
        return "ok"

    result = await AntiSpamMiddleware()(handler, event, {})

    assert result == "ok"
    assert called
