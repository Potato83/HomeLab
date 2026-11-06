# Настройка и управление

Этот гайд описывает локальную разработку и безопасную эксплуатацию трёх ботов. Актуальные серверные особенности сверяйте с README и `docs/release-status.md` внутри приложения.

## 1. Получить исходники

После создания закрытого GitHub-репозитория: `git clone <SSH URL>` и `cd HomeLab`. Проверяйте, что репозиторий имеет Private visibility и SSH URL указывает на ваш аккаунт. Пока публикация не выполнена, этот локальный каталог — подготовка, а не удалённая копия.

## 2. Настроить один бот локально

Работайте из нужного каталога приложения, чтобы его зависимости и команды не смешивались. Python 3.12+. Для каждого бота свой `.venv`, `.env` и данные.

### ServerBot

```sh
cd apps/bots/serverbot
cp .env.example .env
chmod 600 .env
# заполнить BOT_TOKEN и ALLOWED_USER_IDS в локальном редакторе
uv sync --locked
uv run --locked python -m serverbot.main
```

### ChatBot

```sh
cd apps/bots/chatbot
cp .env.example .env
chmod 600 .env
# заполнить BOT_TOKEN и ADMIN_IDS
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.lock
python -m potato_bot.main
```

### BotWork

```sh
cd apps/bots/workbot
cp .env.example .env
chmod 600 .env
# заполнить TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID
python -m venv .venv
. .venv/bin/activate
python -m pip install --require-hashes -r requirements.txt
python -m botwork run
```

Не коммитьте `.env`; `.env.example` содержит только пустые поля и примеры. Не запускайте локально вторую копию боевого токена одновременно с сервером: Telegram polling обычно допускает один активный экземпляр.

## 3. Проверки перед публикацией

CI выполняет тесты и проверки зависимостей для каждого приложения. Локальные команды есть в README конкретного бота. Перед `git add` проверяйте `git status --short`, `git diff --cached`, `git check-ignore -v apps/bots/*/.env` и размер добавляемых файлов. Не добавляйте `work/`, `.venv`, `.env`, базы или журналы.

## 4. Серверная эксплуатация

Все три бота уже работают на Alpine как отдельные OpenRC-службы. От администратора сервера: `rc-service serverbot status`, `rc-service chatbot status`, `rc-service botwork status`; перезапуск: `rc-service <service> restart`. Журналы находятся в `/var/log/<service>/`, код — `/srv/bots/<service>`. Секреты и рабочие данные остаются на сервере.

Не обновляйте сервер из этого репозитория автоматически. Перед ручным развёртыванием сравните точечные изменения приложения, сохраните текущий код и данные согласно действующей процедуре бота, примените один бот, проверьте статус и журналы и только затем переходите к следующему. Не меняйте `.env` сервера копированием из ноутбука.

## 5. Доступ и публикация

CI имеет только `contents: read`; workflow не содержит деплоя и не требует секретов. После проверки истории и staged diff репозиторий можно создать с Private visibility. Если секрет обнаружен в staged content или истории, публикацию остановить, отозвать секрет и очистить историю до первого push.

## 6. Откат

При проблеме после обновления остановите только затронутую службу, верните её прежнюю версию кода и запустите снова. Убедитесь в `rc-service <service> status` и посмотрите последние строки журнала. Не удаляйте каталоги данных и `.env` при откате.
