# Развёртывание

Бот запускается внутри Linux-контейнера с Python. Alpine остаётся ОС хоста. Контейнер собирается из Dockerfile на базе Debian slim, что не требует устанавливать зависимости Python непосредственно в Alpine.

## Alpine и Podman

Выполняйте команды из корня проекта. `.env` создаётся из `.env.example`; токен заполните у себя и ограничьте чтение файла владельцем. Используйте одного и того же пользователя Podman для всех команд.

```sh
podman build -t localhost/botwork:0.17.0 .
test -e .env || cp .env.example .env
chmod 600 .env
podman run --rm localhost/botwork:0.17.0 python -m botwork demo
podman run --rm localhost/botwork:0.17.0 python -m botwork preview
```

Получение chat_id после заполнения токена и отправки `/start` вашему боту:

```sh
podman run --rm --env-file .env \
  --read-only --cap-drop=ALL --security-opt=no-new-privileges \
  --pids-limit=64 --memory=512m --cpus=1.0 \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m \
  localhost/botwork:0.17.0 python -m botwork chat-id
```

Запишите ID в `.env`. Создайте постоянный том и контейнер:

```sh
podman volume create botwork-data
# Проверка с тем же томом и конфигурацией, что будут у постоянного процесса:
podman run --rm --env-file .env \
  --read-only --cap-drop=ALL --security-opt=no-new-privileges \
  --pids-limit=64 --memory=512m --cpus=1.0 \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m \
  -e BOTWORK_DB=/data/botwork.sqlite3 \
  -v botwork-data:/data:U \
  -v "$PWD/config.toml":/app/config.toml:ro \
  localhost/botwork:0.17.0 python -m botwork doctor --online
# Отдельно отправить одно настоящее тестовое сообщение:
podman run --rm --env-file .env \
  --read-only --cap-drop=ALL --security-opt=no-new-privileges \
  --pids-limit=64 --memory=512m --cpus=1.0 \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m \
  localhost/botwork:0.17.0 python -m botwork test-telegram
podman run -d --name botwork \
  --env-file .env \
  --read-only --cap-drop=ALL --security-opt=no-new-privileges \
  --pids-limit=64 --memory=512m --cpus=1.0 \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m \
  -e BOTWORK_DB=/data/botwork.sqlite3 \
  -v botwork-data:/data:U \
  -v "$PWD/config.toml":/app/config.toml:ro \
  --restart=unless-stopped \
  --stop-timeout=90 \
  localhost/botwork:0.17.0
podman logs -f botwork
```

`:U` меняет владельца файлов **выделенного именованного тома** под пользователя контейнера; для каталога исходников этот флаг не используется. Подробнее: [документация томов Podman](https://docs.podman.io/en/latest/markdown/podman-run.1.html).

Редиректы HTTP-клиента отключены; исходящие подключения к частным и служебным адресам должны блокироваться политикой хоста или egress-прокси. Ограничьте исходящий HTTPS адресами Telegram и включённых источников, а DNS — доверенным резолвером. Compose и параметры контейнера сами по себе не задают фильтр адресов назначения.

Посмотреть состояние или остановить:

```sh
podman exec botwork python -m botwork status
podman exec botwork python -m botwork sources
podman stop botwork
podman start botwork
```

Изменение `config.toml` требует `podman restart botwork`. Изменение переменных из `.env` требует пересоздания контейнера с тем же томом.

`--restart` управляет перезапуском контейнера при завершении процесса; для запуска после перезагрузки Alpine настройте службу Podman/OpenRC в вашей среде. Сам по себе флаг не заменяет проверку автозапуска хоста. Для rootful Podman можно использовать пример `deploy/botwork-podman.openrc`, описанный ниже. Rootless Podman требует службы от имени того же пользователя; пример rootful к нему не применяется.

Базовый Python-образ закреплён по digest, а зависимости устанавливаются только по закреплённым версиям и хешам. При плановом обновлении образа обновляйте digest и хеши вместе с версиями пакетов.

### Пример OpenRC для rootful Podman

Сначала создайте контейнер командами выше от root. Не включайте параллельно другие службы, которые управляют этим контейнером.

```sh
install -m 755 deploy/botwork-podman.openrc /etc/init.d/botwork
rc-update add botwork default
rc-service botwork start
```

Скрипт запускает существующий контейнер через `podman start` и не меняет его параметры; создайте контейнер по команде выше с ограничениями прав и ресурсов. Убедитесь, что `/usr/bin/podman` — верный путь на сервере. После установки проверьте `rc-service botwork status`, логи и фактический запуск после перезагрузки. Скрипт подготовлен, но в OpenRC-среде пока не исполнялся.

## Ubuntu и Docker Compose

Создайте `.env` из примера, заполните токен и ограничьте чтение файла владельцем:

```sh
test -e .env || cp .env.example .env
chmod 600 .env
docker compose build
docker compose run --rm botwork python -m botwork demo
docker compose run --rm botwork python -m botwork preview
# Отправьте вашему боту /start:
docker compose run --rm botwork python -m botwork chat-id
# Запишите TELEGRAM_CHAT_ID в .env:
docker compose run --rm botwork python -m botwork doctor --online
# Эта команда отправит одно настоящее тестовое сообщение:
docker compose run --rm botwork python -m botwork test-telegram
docker compose up -d
docker compose logs -f
```

Состояние и остановка:

```sh
docker compose exec botwork python -m botwork status
docker compose exec botwork python -m botwork sources
docker compose down
```

Именованный том сохраняется при обычном `down`. Команда `down -v` удалит базу и историю отправок. Для автозапуска после перезагрузки должен быть включён сам Docker; политика контейнера — `unless-stopped`.

## Резервная копия без остановки (с версии 0.6)

Создайте копию внутри постоянного тома, затем перенесите готовый файл на хост:

```sh
podman exec botwork python -m botwork backup --output /data/backups/botwork-001.sqlite3
mkdir -p backups
podman cp botwork:/data/backups/botwork-001.sqlite3 ./backups/botwork-001.sqlite3
```

Для Docker Compose:

```sh
docker compose exec botwork python -m botwork backup --output /data/backups/botwork-001.sqlite3
mkdir -p backups
docker compose cp botwork:/data/backups/botwork-001.sqlite3 ./backups/botwork-001.sqlite3
```

Каждый раз задавайте новое имя. Существующие файлы команда не заменяет. Копия проверяется SQLite и сохраняется одним файлом без отдельного WAL; вывод содержит размер и SHA-256, а `--json` позволяет сохранить отчёт. Для проверки перенесённого файла можно использовать `sha256sum backups/botwork-001.sqlite3` и сравнить с выводом команды.

Копия в том же томе зависит от этого тома: сохраняйте готовые копии также вне контейнера/сервера. `config.toml` и `.env` в копию SQLite не входят. Команда работает параллельно с постоянным процессом и не меняет историю отправок.

### Копирование при обновлении с версии 0.5 или более ранней

В старом работающем образе команды `backup` ещё нет. Соберите новый образ и выполните его команду как отдельный процесс с тем же томом:

```sh
podman build -t localhost/botwork:0.17.0 .
podman run --rm \
  --read-only --cap-drop=ALL --security-opt=no-new-privileges \
  --pids-limit=64 --memory=512m --cpus=1.0 \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m \
  -e BOTWORK_DB=/data/botwork.sqlite3 \
  -v botwork-data:/data:U \
  localhost/botwork:0.17.0 python -m botwork backup --output /data/backups/before-update-001.sqlite3
mkdir -p backups
podman cp botwork:/data/backups/before-update-001.sqlite3 ./backups/before-update-001.sqlite3
```

Для Docker Compose:

```sh
docker compose build
docker compose run --rm botwork python -m botwork backup --output /data/backups/before-update-001.sqlite3
mkdir -p backups
docker compose cp botwork:/data/backups/before-update-001.sqlite3 ./backups/before-update-001.sqlite3
```

Постоянный процесс старой версии при этом может продолжать работать. Используется API SQLite с учётом WAL; простое копирование одного файла работающей базы не заменяет этот шаг. После сохранения готовой копии остановите старый экземпляр и продолжайте обновление.

## Восстановление в новый путь

1. Остановите постоянный процесс и сделайте копию текущего состояния.
2. Перенесите выбранную резервную копию в новый файл внутри постоянного тома, например `/data/restored.sqlite3`; не заменяйте открытую базу работающего процесса.
3. Задайте `BOTWORK_DB=/data/restored.sqlite3` в настройках запуска контейнера, пересоздайте контейнер с тем же томом и проверьте `doctor`.
4. Запустите бот и проверьте `/status`. История соответствует моменту копии: уведомления, отправленные после него, могут повториться.

Для Podman меняйте `-e BOTWORK_DB=...` в команде `podman run`, для Compose — значение `BOTWORK_DB` в `compose.yaml` (оно имеет приоритет над `.env`). `config.toml` и токен восстановите отдельно. Новый путь позволяет сохранить прежнюю базу и её служебные файлы нетронутыми.

## Проверка после запуска

- `/status` показывает успешную проверку включённых источников либо конкретную ошибку.
- Подходящие существующие объявления приходят после первого успешного сбора. Отсутствие сообщений при успешном сборе возможно, если совпадений нет.
- Перезапуск контейнера сохраняет историю.
- `/check` запускает внеплановую проверку; повторный ручной запуск ограничен разом в минуту.
- Доступ к сайтам и Telegram проверяйте именно с сервера: его сеть может отличаться от среды разработки.

## Обновление с предыдущей версии

Соберите новый образ и сделайте резервную копию базы описанным выше способом. Затем остановите старый контейнер и пересоздайте его с прежним именованным томом. История вакансий сохраняется; таблица состояния сбоев добавляется автоматически. Для Docker Compose после сборки и резервной копии выполните `docker compose up -d`.

Для Podman остановите и удалите только старый контейнер, сохранив именованный том, затем выполните команду `podman run` из инструкции с образом `localhost/botwork:0.17.0`. Если используется OpenRC, сначала остановите службу, затем пересоздайте контейнер и снова запустите службу. Параметры `[alerts]`, `event_audience` и `event_updates` необязательны для старого `config.toml`: используются безопасные значения по умолчанию, все мероприятия остаются в поиске.

Проверьте `/sources` и `/status` после запуска. При штатном обновлении прежние подтверждённые сообщения не приходят повторно. Для проверки уведомлений о сбоях предусмотрены автоматические тесты с имитацией времени; нарушать доступ к настоящим источникам для проверки не требуется.

## Диагностика

`doctor` без `--online` не обращается к сайтам и Telegram. Для проверки только Telegram добавьте `--online --skip-sources`. Команда проверяет доступ к хранилищу с правами пользователя контейнера и не меняет историю отправок; это позволяет проверить том до первого запуска мониторинга.

Для работающего контейнера можно выполнить `podman exec botwork python -m botwork doctor --online --skip-sources` или `docker compose exec botwork python -m botwork doctor --online --skip-sources`. Диагностика не вызывает `getUpdates` и не забирает команды у постоянного процесса. Она обнаруживает конфликт с webhook. `run`, `once` и `chat-id` дополнительно защищены общей блокировкой в каталоге базы. Две разные базы/сервера с одним токеном эта блокировка не обнаружит; используйте один постоянный экземпляр на токен. Для проверки работающего бота используйте `/check`, а не второй `once`.

## Блокировка процесса

Блокировка берётся до открытия/обновления базы и удерживается весь срок работы `run`. Она сохраняется и между обходами сайтов, пока бот ждёт Telegram. После штатной остановки или аварийного завершения ОС освобождает блокировку автоматически. Lock-файл остаётся на диске; удалять его для перезапуска не нужно. При обновлении остановите прежнюю версию перед запуском новой: версии до 0.6 не участвовали в этой блокировке.
