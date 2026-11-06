# WorkBot

Личный мониторинг вакансий и мероприятий.

```sh
cp .env.example .env
uv sync --locked
uv run bot
```

`BOT_TOKEN`, `OWNER_ID` — единственный владелец. `config.toml` задаёт источники и фильтры.

Проверки: `uv run pytest`. Код: `src/workbot/`, тесты: `tests/`, состояние: `data/`.

[Поиск вакансий](docs/job-search.md) · [Интерфейс](docs/telegram-interface.md) · [Установка и службы](../../../docs/setup-and-operations.md)
