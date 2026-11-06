# ChatBot

Групповые игры, экономика, магазин и модерация.

```sh
cp .env.example .env
uv sync --locked
uv run bot
```

`BOT_TOKEN`, `ADMIN_IDS` — Telegram ID через запятую. `PROXY_URL` необязателен.

Проверки: `uv run pytest`. Код: `src/chatbot/`, тесты: `tests/`, состояние: `data/`.

[Команды и поведение](docs/guide.md) · [Установка и службы](../../../docs/setup-and-operations.md)
