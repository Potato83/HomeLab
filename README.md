# HomeLab

Личный проект эксплуатации Alpine-сервера: Telegram-боты, AI gateway, мониторинг и документация. Публичный проект для портфолио.

- `apps/bots/`: ServerBot, ChatBot, WorkBot и AgentBot.
- `services/omniroute/`: описание AI gateway и безопасные примеры конфигурации.
- `infra/alpine/`: особенности текущей ОС. Другие ОС добавляются при реальной миграции.
- `docs/`: архитектура, настройка, эксплуатация.

[Настройка и управление](docs/setup-and-operations.md) · [Архитектура](docs/architecture.md) · [Закрытый CTF-проект](docs/ctf-harness.md)

Все работающие боты: `uv sync --locked`, затем `uv run bot`.
