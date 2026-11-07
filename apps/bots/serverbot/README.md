# ServerBot

Мониторинг хоста, датчиков, служб и журналов. Компактная Telegram-панель для Kali,
ботов и OmniRoute: запуск, остановка, перезапуск и последние ошибки.

```sh
cp .env.example .env
uv sync --locked
uv run bot
```

`BOT_TOKEN`, `ALLOWED_USER_IDS` — Telegram ID через запятую. `config.toml` задаёт проверки и пороги.

Проверки: `uv run pytest`. Код: `src/serverbot/`, тесты: `tests/`, состояние: `data/`.

[Настройки](docs/guide.md) · [Панель управления](docs/control-panel.md) · [Мониторинг Alpine](docs/host-monitoring.md) · [Установка и службы](../../../docs/setup-and-operations.md)
