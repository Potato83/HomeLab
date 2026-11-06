# Запуск и управление

Python 3.12+ и uv 0.13.7. Команды одинаковы для serverbot, chatbot и workbot:

```sh
cd apps/bots/chatbot
cp .env.example .env
# Заполнить .env; настройки каждого бота описаны в его README.
uv sync --locked
uv run bot
```

`uv run bot --help` — справка. `uv run pytest` — тесты. AgentBot пока не реализован.

## Alpine

Проект размещается в `/srv/homelab/apps/bots/<имя>`, запускается отдельным пользователем `<имя>`. Установите uv и Python, создайте пользователя, затем:

```sh
cd /srv/homelab/apps/bots/chatbot
uv sync --locked --no-dev --python /usr/bin/python3
install -m 755 deploy/chatbot.openrc /etc/init.d/chatbot
rc-update add chatbot default
rc-service chatbot start
```

Код и `.venv` принадлежат root. Пользователю бота доступны запись в `data/` и `/var/log/<имя>/`, чтение `.env` с правами `0640 root:<имя>`. Создайте эти каталоги перед запуском. Шаблон службы использует `/usr/bin/uv`; другой путь задаётся через `uv_path` в `/etc/conf.d/<имя>`.

Статус: `rc-service chatbot status`. Перезапуск: `rc-service chatbot restart`. Журналы: `/var/log/chatbot/`. Подставляйте имя нужного бота.

При обновлении остановите службу, замените код, выполните `uv sync --locked --no-dev --python /usr/bin/python3`, запустите службу и проверьте журнал. Данные остаются в `data/`. При обновлении общего checkout сначала остановите все три службы, затем выполните `git pull`, синхронизируйте зависимости каждого бота и запустите службы.

Для перехода со старой установки перенесите код и данные; старое имя службы WorkBot — `botwork`. Переменные: ServerBot `VPN` → `PROXY_URL`; WorkBot `BOTWORK_*` → `WORKBOT_*`, `TELEGRAM_BOT_TOKEN` → `BOT_TOKEN`, `TELEGRAM_CHAT_ID` → `OWNER_ID`. Новый путь состояния ServerBot — `data/state.json`, базы ChatBot — `data/chatbot.sqlite3`. Структура таблиц SQLite не меняется.

Рабочие `.env`, `data/` и `.venv/` находятся рядом с кодом и исключены из Git. Личные настройки храните в игнорируемом `config.local.toml`: ServerBot выбирает его через `SERVERBOT_CONFIG`, WorkBot — через `WORKBOT_CONFIG` в `.env`. Отслеживаемый `config.toml` остаётся примером.
