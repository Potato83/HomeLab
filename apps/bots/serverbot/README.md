# ServerBot

Мониторинг хоста, датчиков, служб и журналов.

```sh
cp .env.example .env
uv sync --locked
uv run bot
```

`BOT_TOKEN`, `ALLOWED_USER_IDS` — Telegram ID через запятую. `config.toml` задаёт проверки и пороги.

Проверки: `uv run pytest`. Код: `src/serverbot/`, тесты: `tests/`, состояние: `data/`.

[Настройки](docs/guide.md) · [Мониторинг Alpine](docs/host-monitoring.md) · [Установка и службы](../../../docs/setup-and-operations.md)
