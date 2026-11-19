# sing-box на Alpine

На HomeLab OpenRC запускает sing-box 1.13.11 при загрузке с параметром
`-C /etc/sing-box`. Этот каталог содержит рабочий `config.json`. Вход
`mixed` принимает HTTP CONNECT и SOCKS5 на `127.0.0.1:2080`.
Маршрут к частным IP выбирает `direct`, остальные запросы идут в группу
`urltest`. Она периодически проверяет внешние узлы и выбирает доступный
быстрый маршрут.

```text
служба → 127.0.0.1:2080 → sing-box → urltest → внешний узел
                                ↳ direct → частный IP
```

Пакет Alpine хранит настройки запуска OpenRC в `/etc/conf.d/sing-box`.
Узлы и их параметры хранятся в рабочем JSON на сервере. В Git достаточно
схемы маршрутов и примеров переменных.

ChatBot и ServerBot принимают `PROXY_URL=socks5://127.0.0.1:2080`.
WorkBot и его HTTP-клиент используют стандартные переменные окружения.
Пример для настройки его `.env` с прямым доступом к российским доменам:

```dotenv
HTTP_PROXY=http://127.0.0.1:2080
HTTPS_PROXY=http://127.0.0.1:2080
NO_PROXY=localhost,127.0.0.1,.ru,.xn--p1ai
```

`NO_PROXY` исключает эти адреса ещё на стороне WorkBot. Проверка TLS
источников настраивается отдельно в [WorkBot](../../apps/bots/workbot/docs/parsers.md).

Проверка конфигурации, службы и HTTP-прокси:

```sh
sing-box check -C /etc/sing-box
rc-service sing-box status
curl -sS -o /dev/null -w '%{http_code}\n' --max-time 15 \
  --noproxy '' --proxy http://127.0.0.1:2080 https://api.telegram.org/
```

Команда curl проверяет внешний маршрут через Telegram. Ветку `direct`
можно проверить через закрытый порт OmniRoute на том же хосте.

```sh
curl -fsS --max-time 5 --noproxy '' --proxy http://127.0.0.1:2080 \
  http://127.0.0.1:20127/api/monitoring/health
```

Настройки клиентов и пути к данным описаны в [инструкции HomeLab](../../docs/setup-and-operations.md).
