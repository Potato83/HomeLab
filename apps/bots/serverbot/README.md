# ServerBot

Мониторинг Alpine: ресурсы, датчики, разрешённые журналы и уведомления владельцу.

Меню `/start`, `/status`, `/logs` публикуется в Telegram при старте. `/status` показывает короткую сводку для телефона; по кнопке «Подробнее» открываются четыре раздела с переключением внутри сообщения.

## Локальный запуск

Python 3.12+. Команды выполняются из корня проекта. Скопируйте `.env.example` в `.env`, установите права `0600` и заполните `BOT_TOKEN, ALLOWED_USER_IDS`. Используйте отдельный токен для каждого бота.

```sh
test -e .env || cp .env.example .env
chmod 600 .env
uv sync --locked
uv run --locked python -m serverbot.main
```

## Проверки

```sh
uv run --locked pytest src/tests
```

Результаты и ограничения текущей проверки: [статус подготовки](docs/release-status.md).

## Эксплуатация

На HomeLab бот запущен как OpenRC-служба `serverbot` под отдельным пользователем. Код — `/srv/bots/serverbot`, секреты — `.env`, журналы — `/var/log/serverbot/`.

От root на сервере:

```sh
rc-service serverbot status
rc-service serverbot restart
```

Подробности запуска, проверок, данных и отката: [состояние выпуска](docs/release-status.md). Запускайте одну копию бота. Перед обновлением остановите службу и сохраните базу/состояние вместе с предыдущей версией кода.

## Документация

- [Подробное руководство](docs/guide.md)
- [Безопасность](SECURITY.md)
- [История изменений](CHANGELOG.md)
- [Мониторинг HomeLab и автообновления APK](docs/host-monitoring.md)

## GitHub

Этот бот находится в монорепозитории `HomeLab`; общая публикация и проверка описаны в корневых `README.md` и `docs/setup-and-operations.md`. Не добавляйте `.env`, базы, журналы, `work/` или виртуальное окружение.
