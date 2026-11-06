# ChatBot

Telegram-бот для групп: экономика, игры, магазин и модерация с постоянной SQLite-базой.

Обычные команды видны в чатах; `/admin` и `/add` добавляются в командное меню групп и личных чатов администраторов.

## Локальный запуск

Python 3.12+. Команды выполняются из корня проекта. Скопируйте `.env.example` в `.env`, установите права `0600` и заполните `BOT_TOKEN, ADMIN_IDS`. Для нескольких админов укажите Telegram ID через запятую. Старый `MY_ID` поддерживается, если `ADMIN_IDS` пуст или отсутствует. Используйте отдельный токен для каждого бота.

```sh
test -e .env || cp .env.example .env
chmod 600 .env
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.lock
python -m potato_bot.main
```

## Проверки

```sh
python -m pip install --require-hashes -r requirements-dev.lock
python -m pytest tests
```

Результаты и ограничения текущей проверки: [статус подготовки](docs/release-status.md).

## Эксплуатация

На HomeLab бот запущен как OpenRC-служба `chatbot` под отдельным пользователем. Код — `/srv/bots/chatbot`, секреты — `.env`, журналы — `/var/log/chatbot/`.

От root на сервере:

```sh
rc-service chatbot status
rc-service chatbot restart
```

Подробности запуска, проверок, данных и отката: [состояние выпуска](docs/release-status.md). Запускайте одну копию бота. Перед обновлением остановите службу и сохраните базу/состояние вместе с предыдущей версией кода.

## Документация

- [Подробное руководство](docs/guide.md)
- [Безопасность](SECURITY.md)
- [История изменений](CHANGELOG.md)

## GitHub

Этот бот находится в монорепозитории `HomeLab`; общая публикация и проверка описаны в корневых `README.md` и `docs/setup-and-operations.md`. Не добавляйте `.env`, базы, журналы, `work/` или виртуальное окружение.
