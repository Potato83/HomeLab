# BotWork

Личный мониторинг вакансий и мероприятий с фильтрами, каталогом и уведомлениями в Telegram.

## Локальный запуск

Python 3.12+. Команды выполняются из корня проекта. Скопируйте `.env.example` в `.env`, установите права `0600` и заполните `TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID`. Используйте отдельный токен для каждого бота.

BotWork личный: `TELEGRAM_CHAT_ID` одновременно задаёт чат уведомлений и владельца, чьи команды принимаются. Добавить других администраторов через `.env` сейчас нельзя. Меню основных slash-команд настраивается для этого личного чата при старте.

```sh
test -e .env || cp .env.example .env
chmod 600 .env
python -m venv .venv
. .venv/bin/activate
python -m pip install --require-hashes -r requirements.txt
python -m botwork run
```

## Проверки

```sh
python -m unittest discover -s tests -v
```

Результаты и ограничения текущей проверки: [статус подготовки](docs/release-status.md).

## Эксплуатация

На HomeLab бот запущен как OpenRC-служба `botwork` под отдельным пользователем. Код — `/srv/bots/botwork`, секреты — `.env`, журналы — `/var/log/botwork/`.

От root на сервере:

```sh
rc-service botwork status
rc-service botwork restart
```

Подробности запуска, проверок, данных и отката: [состояние выпуска](docs/release-status.md). Запускайте одну копию бота. Перед обновлением остановите службу и сохраните базу/состояние вместе с предыдущей версией кода.

## Документация

- [Подробное руководство](docs/guide.md)
- [Безопасность](SECURITY.md)
- [История изменений](CHANGELOG.md)

## GitHub

Этот бот находится в монорепозитории `HomeLab`; общая публикация и проверка описаны в корневых `README.md` и `docs/setup-and-operations.md`. Не добавляйте `.env`, базы, журналы, `work/` или виртуальное окружение.
