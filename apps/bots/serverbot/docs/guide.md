# ServerBot

A read-only Telegram bot for monitoring a home server.

- `/start`: show available commands.
- `/status`: one-message summary of disks, CPU, RAM, load, uptime, temperatures, battery, processes, network reachability, Docker, backups and HTTP services.
- `/logs <name> [lines]`: last lines from a configured log file; only configured aliases are accepted.
- `/status` includes **Refresh** and **Details** buttons; Details shows per-disk usage.
- Daily report at **08:00 Europe/Moscow**, sent to configured recipients.
- Optional interval digest; a 24-hour cadence uses the daily 08:00 report.
- Background checks every 5 minutes, with disk, CPU, RAM, temperature, battery, process, backup and HTTP alerts on state changes and separate recovery messages.
- Private-chat access restricted to a whitelist of Telegram user IDs.
- Access attempts and scheduled delivery results are logged to stderr, without message contents or secret values.

## Setup

Requires Python 3.12+ and uv. From the repository root:

```sh
uv sync --locked
cp .env.example .env
```

Edit `.env` with your own values:

```dotenv
BOT_TOKEN=your-monitoring-bot-token
ALLOWED_USER_IDS=123456789,987654321
DAILY_REPORT_IDS=123456789
VPN=socks5://127.0.0.1:2080
```

`ALLOWED_USER_IDS` must contain positive IDs separated by commas. An empty or invalid whitelist prevents startup. The previous `ADMIN_ID` setting is supported when `ALLOWED_USER_IDS` is absent.

Leave `DAILY_REPORT_IDS` empty to send the daily report to everyone in the whitelist, or specify a subset. Each recipient must first start the bot in a private chat. Group chats are blocked to avoid exposing reports to other members.

`VPN` is optional: leave it empty for a direct Telegram connection. Local HTTP checks connect directly and do not use this proxy.

Adjust `config.toml` for your server, then run:

```sh
uv run --locked python -m serverbot.main
```

Restart the bot after changing `.env` or `config.toml`. Run one instance to avoid duplicate daily reports. The scheduler works while the process is running; reports missed while it is stopped are not replayed after restart.

## State-change alerts

The bot checks configured disks, CPU, RAM, temperatures, batteries, processes, backups and HTTP endpoints at startup, then every 5 minutes. Alerts go to all users in `ALLOWED_USER_IDS`; `DAILY_REPORT_IDS` controls scheduled summaries.

Checks have four states: `ok`, `warning`, `critical` and `error` (a reading could not be obtained). Values at a threshold already count as warning or critical. Unexpected HTTP statuses are critical; timeouts and connection failures are errors.

Unchanged states do not send more messages. A transition back to `ok` sends a separate `RECOVERED` message. On the first run, healthy checks are recorded silently and existing problems are reported once. A move from critical to warning is still an alert, since the check has not fully recovered.

## Battery and external power

Battery checks read Linux power-supply attributes directly under `/sys/class/power_supply`; they do not use commands or privileged operations. `battery_supply = "auto"` selects the only detected battery. `charger_supply = "auto"` checks online charger supplies (Mains/USB). Set either field to a supply directory name when automatic detection is ambiguous.

An unplugged charger raises a warning immediately, regardless of battery percentage. Charge at or below 50% raises a warning; 20% or lower is critical. The report also shows the kernel battery status (`Charging`, `Discharging` or `Not charging`) rather than guessing it from the percentage. Alerts are sent only when the state changes. Reconnecting power recovers the power check; a low-charge warning recovers after the charge rises above 50%.

```toml
[[checks.battery]]
name = "Server battery"
battery_supply = "auto"
charger_supply = "auto"
warning_percent = 50
critical_percent = 20
```

If the server has no battery, omit this entry. The service account needs read access to the configured attributes.

Configure the interval and state location in TOML:

```toml
[monitoring]
interval_seconds = 300
state_file = "var/state.json"
```

Relative state paths are resolved beside `config.toml`. State is saved per recipient in JSON using a temporary file and atomic replacement. `var/` is excluded from Git. An unreadable or invalid state file stops startup instead of resetting alert history; a new missing file starts with empty history. Startup also checks that the state can be written.

A failed delivery is retried on the next check without repeating deliveries already recorded for other recipients. If the process stops after Telegram accepts a message but before its state is saved, the message can be repeated after restart. Only run one instance against a given state file.

CPU and RAM warnings and critical thresholds are set in `[system]` in `config.toml`. CPU readings sample `/proc` over a short interval; RAM uses `MemAvailable`. Temperature alerts continue to use the configured hwmon sensors. Add a `[[checks.process]]` entry with the exact Linux process name to alert if it stops running:

```toml
[[checks.process]]
name = "sshd"
```

Process names are matched against `/proc` process names and executable names. A missing configured process is critical and a later return sends a recovery message.

## Network, Docker, logs, and digests

Network checks are currently commented out in `config.toml`. When enabled, `network.ping_host` can be a hostname or IP address; the bot invokes one bounded `ping` command without a shell. The optional `network.external_ip_url` remains blank by default. Behind another server or NAT, a public-IP lookup reports the network's egress address, not an address assigned to the laptop.

`/status` lists containers from `docker ps -a`. If the Docker CLI is absent, the report says so and continues. Listing requires the service account to have access to the Docker socket; adding the account to the Docker group is effectively granting root-level access, so configure it only if that is acceptable.

`/logs <name> [lines]` accepts only aliases under `[[logs.files]]`; it never accepts a filesystem path from Telegram. `logs.allowed_directories` is the path allowlist, and symlinks are checked again when read. The requested line count is bounded by `logs.max_lines`, and `/status`, Refresh, and `/logs` are rate-limited per user.

The daily report runs at `[digest].hour` in Europe/Moscow time; it is set to `8`. `[digest].interval_hours = 24` uses this daily schedule. Other values from `1` to `168` add a separate interval digest, starting after one full interval; `0` disables that extra digest.

The log aliases in `config.toml` include the ServerBot output and error files defined by the OpenRC service, plus Alpine's usual `/var/log/messages` syslog file. Try `/logs system 50` for system logs, `/logs serverbot 50` for bot output, or `/logs serverbot-error 50` for bot errors. Alpine's BusyBox syslog documentation identifies `/var/log/messages` as the file to read; if syslog is configured for memory-only logging with `-C`, use of that file may not apply. Each configured file must be readable by the `serverbot` account. ([Alpine Linux syslog documentation](https://wiki.alpinelinux.org/wiki/Syslog))

## Backup freshness

ServerBot can check the modification time of a marker file written by a successful backup job. It does not inspect or create backups. Update the marker only after the backup command and any integrity check have succeeded; a failed or interrupted backup must leave its timestamp unchanged.

Before enabling a marker check, configure the backup job and verify that a restore works. A mounted backup destination alone does not prove that a usable snapshot exists.

Add one `[[checks.backup]]` entry per backup set:

```toml
[[checks.backup]]
name = "Home backup"
marker_file = "/var/lib/backups/home-success.marker"
warning_after_hours = 30
critical_after_hours = 48
```

Relative marker paths are resolved beside `config.toml`. The marker should be readable by the `serverbot` service account. `/status` and the daily report show the last successful marker time in UTC and its age. A missing or unreadable marker is an error; an old marker produces warning and critical states at the configured ages.

## Alpine/OpenRC deployment

The service template is [`deploy/serverbot.openrc`](../deploy/serverbot.openrc). It runs the project virtualenv as the unprivileged `serverbot` account and expects the project in `/opt/serverbot` by default.

1. Install Python, `uv`, and the project files in `/opt/serverbot`. Create the `serverbot` account. The account needs read/execute access to the code and virtualenv, and write access only to `var/` and the log directory.
2. Set the real Telegram values in `.env`; configure disk paths, health endpoints, and backup marker paths in `config.toml`. Keep `.env` readable only by root and the service account (for example, owner `root:serverbot`, mode `640`) or by the service account alone (mode `600`).
3. Create the state and log directories, then install dependencies into the project virtualenv:

   ```sh
   install -d -o serverbot -g serverbot /opt/serverbot/var /var/log/serverbot
   cd /opt/serverbot
   uv sync --locked --no-dev
   ```

   Make `/var/log/serverbot` writable by the service account. The project and virtualenv can remain root-owned and read-only to that account.

4. Install the service and enable it at boot:

   ```sh
   install -m 0755 deploy/serverbot.openrc /etc/init.d/serverbot
   rc-update add serverbot default
   rc-service serverbot start
   rc-service serverbot status
   ```

5. After the first start, check `rc-service serverbot status`, the service logs, and `/status` in Telegram. Confirm the alerts and state file survive a reboot before relying on monitoring.

Set `SERVERBOT_HOME`, `SERVERBOT_USER`, `SERVERBOT_GROUP`, or `SERVERBOT_LOG_DIR` in `/etc/conf.d/serverbot` if the installation uses different paths or account names.

## HTTP checks

Add one `[[checks.http]]` entry per service or other bot's health endpoint:

```toml
[[checks.http]]
name = "Example service"
url = "http://127.0.0.1:8080/health"
timeout = 5.0
expected_status = 200
```

The example URL must be replaced with a real endpoint. HTTP checks are optional; commented examples in `config.toml` are disabled.

The report always includes an `HTTP/API` section. With no configured checks it says `No HTTP checks configured`. Each enabled check shows its HTTP status or request error; a status mismatch also shows the expected code. Response bodies are not included.

A GET request checks the response status. Redirects are not followed and TLS certificates are verified. Timeout must be greater than zero and at most 60 seconds. An unreachable or failing service does not prevent other results from appearing in the report.

For authentication, keep the header value in `.env` and reference its variable name:

```toml
auth_env = "PORTAINER_API_KEY"
auth_header = "X-API-Key"
```

```dotenv
PORTAINER_API_KEY=your-key
```

Do not place secrets in committed URLs or config files. For Podman services, use an endpoint reachable from the monitoring bot. These checks measure HTTP availability, not the container's Podman state. A Telegram bot token does not prove that another bot's local process is running; use a health endpoint that reflects its application state.

## Tests

```sh
uv run --locked pytest
```

Tests use local HTTP servers, temporary sensor files and fake Telegram delivery; they do not send real reports. Existing coverage includes disk and temperature success/error paths, HTTP status errors, actual request timeouts, whitelist enforcement, config validation, Moscow scheduling, state transitions, recovery, delivery retries, atomic state writes and secret redaction. The new `/proc` metrics, process checks, Docker listing, log-tail command, buttons and interval digest are not covered by the current tests.

## Next steps

- Configure and verify the backup job and a restore, then add its marker and the actual service health endpoints for this server.
- Install the OpenRC service on Alpine and confirm startup, permissions, Telegram delivery, and alert recovery after reboot.

The bot performs read-only monitoring. It uses fixed-argument `ping` and `docker ps` commands for status checks; it does not restart services or run arbitrary commands.
