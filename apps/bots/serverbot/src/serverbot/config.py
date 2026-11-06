import ipaddress
import math
import re
import tomllib
from dataclasses import dataclass
from os import getenv
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class DiskMountConfig:
    path: str
    warning: float
    critical: float


@dataclass(frozen=True)
class TemperatureCheckConfig:
    name: str
    source: str
    driver: str
    sensor: str
    warning: float
    critical: float


@dataclass(frozen=True)
class HttpCheckConfig:
    name: str
    url: str
    timeout: float = 5.0
    expected_status: int = 200
    auth_env: str | None = None
    auth_header: str = "Authorization"


@dataclass(frozen=True)
class BackupCheckConfig:
    name: str
    marker_file: Path
    warning_after_hours: float
    critical_after_hours: float


@dataclass(frozen=True)
class BatteryCheckConfig:
    name: str
    battery_supply: str
    charger_supply: str
    warning_percent: float
    critical_percent: float


@dataclass(frozen=True)
class ProcessCheckConfig:
    name: str


@dataclass(frozen=True)
class SystemConfig:
    cpu_warning_percent: float = 80
    cpu_critical_percent: float = 95
    memory_warning_percent: float = 80
    memory_critical_percent: float = 95
    top_processes: int = 5


@dataclass(frozen=True)
class NetworkConfig:
    ping_host: str | None = None
    ping_timeout_seconds: int = 3
    external_ip_url: str | None = None


@dataclass(frozen=True)
class LogFileConfig:
    name: str
    path: Path


@dataclass(frozen=True)
class EnvConfig:
    # Runtime settings from .env; do not print this object because it holds the token
    token: str
    allowed_user_ids: frozenset[int]
    report_user_ids: frozenset[int]
    proxy: str | None


def load_env_config() -> EnvConfig:
    token = getenv("BOT_TOKEN", "").strip()
    if not token:
        raise ConfigError("BOT_TOKEN is required")

    def read_ids(value: str) -> frozenset[int]:
        # Convert "123, 456" to unique IDs and reject an empty or invalid list
        items = value.split(",")
        if any(not item.strip().isdigit() or int(item.strip()) <= 0 for item in items):
            raise ConfigError("User IDs must be positive integers separated by commas")
        return frozenset(int(item.strip()) for item in items)

    # ADMIN_ID is a fallback for the older .env format
    allowed = read_ids(getenv("ALLOWED_USER_IDS", getenv("ADMIN_ID", "")))
    # Empty DAILY_REPORT_IDS means everyone in the whitelist receives reports
    recipients = read_ids(getenv("DAILY_REPORT_IDS", "").strip() or ",".join(map(str, sorted(allowed))))
    # Reports can only be sent to users who are allowed to use the bot
    if not recipients.issubset(allowed):
        raise ConfigError("DAILY_REPORT_IDS must be in ALLOWED_USER_IDS")
    proxy = getenv("PROXY_URL", "").strip() or None
    # Accept the old proxy format "127.0.0.1:2080" as well
    if proxy and "://" not in proxy:
        proxy = "socks5://" + proxy
    return EnvConfig(token, allowed, recipients, proxy)


@dataclass(frozen=True)
class AppConfig:
    disks: tuple[DiskMountConfig, ...]
    temperatures: tuple[TemperatureCheckConfig, ...] = ()
    http_checks: tuple[HttpCheckConfig, ...] = ()
    check_interval_seconds: int = 300
    state_file: Path = Path("data/state.json")
    backups: tuple[BackupCheckConfig, ...] = ()
    batteries: tuple[BatteryCheckConfig, ...] = ()
    processes: tuple[ProcessCheckConfig, ...] = ()
    system: SystemConfig | None = None
    network: NetworkConfig = NetworkConfig()
    log_files: tuple[LogFileConfig, ...] = ()
    log_directories: tuple[Path, ...] = ()
    max_log_lines: int = 200
    command_rate_limit_seconds: int = 10
    digest_interval_hours: int = 0
    digest_hour: int = 8
    host_snapshot: Path | None = None


class ConfigError(ValueError):
    pass

# Func validates and converts a config threshold value
def _threshold(value: object, name: str, mount_path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{name} for {mount_path!r} must be a number from 0 to 100")
    if not 0 <= value <= 100:
        raise ConfigError(f"{name} for {mount_path!r} must be from 0 to 100")
    return float(value)


def _positive_hours(value: object, name: str, check_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{name} for backup {check_name!r} must be a positive number of hours")
    try:
        hours = float(value)
    except OverflowError:
        raise ConfigError(f"{name} for backup {check_name!r} must be a positive number of hours") from None
    if not math.isfinite(hours) or hours <= 0:
        raise ConfigError(f"{name} for backup {check_name!r} must be a positive number of hours")
    return hours

# load toml config and returns AppConfig
def load_config(path: str | Path | None = None) -> AppConfig:
    config_path = Path(path) if path is not None else Path(getenv("SERVERBOT_CONFIG", "config.toml"))
    try:
        with config_path.open("rb") as config_file:
            raw = tomllib.load(config_file)
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"Invalid TOML in {config_path}: {error}") from error
    except OSError as error:
        raise ConfigError(f"Cannot read config file {config_path}: {error}") from error

    defaults_section = raw.get("defaults", {})
    checks_section = raw.get("checks", {})
    if not isinstance(defaults_section, dict) or not isinstance(checks_section, dict):
        raise ConfigError("[defaults] and [checks] must be tables")
    defaults = defaults_section.get("disk", {})
    checks = checks_section.get("disk")
    if not isinstance(defaults, dict) or not isinstance(checks, list) or not checks:
        raise ConfigError("Config must define [[checks.disk]] entries and [defaults.disk]")

    default_warning = defaults.get("warning", 80)
    default_critical = defaults.get("critical", 90)
    default_warning = _threshold(default_warning, "default warning", "all mounts")
    default_critical = _threshold(default_critical, "default critical", "all mounts")
    if default_warning >= default_critical:
        raise ConfigError("default warning must be less than default critical")
    mounts: list[DiskMountConfig] = []
    for index, check in enumerate(checks):
        if not isinstance(check, dict):
            raise ConfigError(f"checks.disk[{index}] must be a table")
        mount_path = check.get("path")
        if not isinstance(mount_path, str) or not mount_path.strip():
            raise ConfigError(f"checks.disk[{index}] must have a non-empty path")

        warning = _threshold(check.get("warning", default_warning), "warning", mount_path)
        critical = _threshold(check.get("critical", default_critical), "critical", mount_path)
        if warning >= critical:
            raise ConfigError(f"warning must be less than critical for {mount_path!r}")
        mounts.append(DiskMountConfig(mount_path, warning, critical))

    temperature_checks = checks_section.get("temperature", [])
    if not isinstance(temperature_checks, list):
        raise ConfigError("checks.temperature must be a list of tables")

    temperatures = []
    for index, check in enumerate(temperature_checks):
        if not isinstance(check, dict):
            raise ConfigError(f"checks.temperature[{index}] must be a table")
        for field in ("name", "driver", "sensor"):
            value = check.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ConfigError(f"checks.temperature[{index}] must have a non-empty {field}")
        if check.get("source") != "hwmon":
            raise ConfigError("Temperature source must be hwmon")
        if not re.fullmatch(r"temp[1-9][0-9]*", check["sensor"]):
            raise ConfigError("Temperature sensor must look like temp1 or temp2")

        warning = check.get("warning")
        critical = check.get("critical")
        for value in (warning, critical):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ConfigError("Temperature thresholds must be finite numbers")
        if warning >= critical:
            raise ConfigError(f"warning must be less than critical for {check['name']!r}")
        temperatures.append(TemperatureCheckConfig(
            name=check["name"], source=check["source"], driver=check["driver"],
            sensor=check["sensor"], warning=float(warning), critical=float(critical),
        ))

    # HTTP checks are optional; each table describes one service endpoint
    http_checks = checks_section.get("http", [])
    if not isinstance(http_checks, list):
        raise ConfigError("checks.http must be a list of tables")
    services = []
    for index, check in enumerate(http_checks):
        if not isinstance(check, dict):
            raise ConfigError(f"checks.http[{index}] must be a table")
        for field in ("name", "url"):
            if not isinstance(check.get(field), str) or not check[field].strip():
                raise ConfigError(f"checks.http[{index}] must have a non-empty {field}")
        try:
            url = urlsplit(check["url"])
            port = url.port
        except ValueError:
            raise ConfigError("Invalid HTTP URL") from None
        if port is not None and not 1 <= port <= 65535:
            raise ConfigError("Invalid HTTP port")
        if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or url.fragment:
            raise ConfigError("HTTP URL must use http/https without embedded credentials or fragments")
        timeout = check.get("timeout", 5.0)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 60:
            raise ConfigError("HTTP timeout must be a number greater than 0 and at most 60 seconds")
        status = check.get("expected_status", 200)
        if isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599:
            raise ConfigError("expected_status must be an HTTP status code")
        auth_env = check.get("auth_env")
        # Store only the variable name in TOML; the secret stays in .env
        if auth_env is not None and (not isinstance(auth_env, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", auth_env)):
            raise ConfigError("auth_env must be an environment variable name")
        auth_header = check.get("auth_header", "Authorization")
        if not isinstance(auth_header, str) or not re.fullmatch(r"[A-Za-z0-9-]+", auth_header):
            raise ConfigError("auth_header must be an HTTP header name")
        services.append(HttpCheckConfig(check["name"], check["url"], float(timeout), status, auth_env, auth_header))

    # A marker's modification time represents the last successful backup
    backup_checks = checks_section.get("backup", [])
    if not isinstance(backup_checks, list):
        raise ConfigError("checks.backup must be a list of tables")
    backups = []
    for index, check in enumerate(backup_checks):
        if not isinstance(check, dict):
            raise ConfigError(f"checks.backup[{index}] must be a table")
        name = check.get("name")
        marker_file = check.get("marker_file")
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(f"checks.backup[{index}] must have a non-empty name")
        if not isinstance(marker_file, str) or not marker_file.strip():
            raise ConfigError(f"checks.backup[{index}] must have a non-empty marker_file")
        warning_after = _positive_hours(check.get("warning_after_hours", 30), "warning_after_hours", name)
        critical_after = _positive_hours(check.get("critical_after_hours", 48), "critical_after_hours", name)
        if warning_after >= critical_after:
            raise ConfigError(f"warning_after_hours must be less than critical_after_hours for backup {name!r}")
        marker_path = Path(marker_file)
        if not marker_path.is_absolute():
            marker_path = config_path.resolve().parent / marker_path
        backups.append(BackupCheckConfig(name, marker_path, warning_after, critical_after))

    battery_checks = checks_section.get("battery", [])
    if not isinstance(battery_checks, list):
        raise ConfigError("checks.battery must be a list of tables")
    batteries = []
    supply_name_pattern = re.compile(r"[A-Za-z0-9_.:-]{1,80}\Z", re.ASCII)
    for index, check in enumerate(battery_checks):
        if not isinstance(check, dict):
            raise ConfigError(f"checks.battery[{index}] must be a table")
        name = check.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(f"checks.battery[{index}] must have a non-empty name")
        battery_supply = check.get("battery_supply", "auto")
        charger_supply = check.get("charger_supply", "auto")
        for field, supply in (("battery_supply", battery_supply), ("charger_supply", charger_supply)):
            if (not isinstance(supply, str) or
                    (supply != "auto" and not supply_name_pattern.fullmatch(supply))):
                raise ConfigError(f"checks.battery[{index}].{field} must be 'auto' or a supply name")
        warning = _threshold(check.get("warning_percent", 50), "warning_percent", name)
        critical = _threshold(check.get("critical_percent", 20), "critical_percent", name)
        if critical >= warning:
            raise ConfigError(f"critical_percent must be less than warning_percent for battery {name!r}")
        batteries.append(BatteryCheckConfig(name, battery_supply, charger_supply, warning, critical))

    process_checks = checks_section.get("process", [])
    if not isinstance(process_checks, list):
        raise ConfigError("checks.process must be a list of tables")
    process_name_pattern = re.compile(r"[A-Za-z0-9_.:/+-]{1,80}\Z", re.ASCII)
    processes = []
    for index, check in enumerate(process_checks):
        if not isinstance(check, dict):
            raise ConfigError(f"checks.process[{index}] must be a table")
        name = check.get("name")
        if not isinstance(name, str) or not process_name_pattern.fullmatch(name):
            raise ConfigError(f"checks.process[{index}].name must be a safe process name")
        processes.append(ProcessCheckConfig(name))

    # Unique names make each saved state refer to exactly one check
    for values in (
        [disk.path for disk in mounts],
        [check.name for check in temperatures],
        [check.name for check in services],
        [check.name for check in backups],
        [check.name for check in batteries],
        [check.name for check in processes],
    ):
        if len(values) != len(set(values)):
            raise ConfigError("Disk paths and names within each check type must be unique")

    monitoring = raw.get("monitoring", {})
    if not isinstance(monitoring, dict):
        raise ConfigError("monitoring must be a table")
    interval = monitoring.get("interval_seconds", 300)
    if isinstance(interval, bool) or not isinstance(interval, int) or not 1 <= interval <= 86400:
        raise ConfigError("interval_seconds must be an integer from 1 to 86400")
    state_file = monitoring.get("state_file", "data/state.json")
    if not isinstance(state_file, str) or not state_file.strip():
        raise ConfigError("state_file must be a non-empty path")
    state_path = Path(state_file)
    if not state_path.is_absolute():
        state_path = config_path.resolve().parent / state_path

    system_section = raw.get("system", {})
    if not isinstance(system_section, dict):
        raise ConfigError("system must be a table")
    cpu_warning = _threshold(system_section.get("cpu_warning_percent", 80), "cpu_warning_percent", "system")
    cpu_critical = _threshold(system_section.get("cpu_critical_percent", 95), "cpu_critical_percent", "system")
    memory_warning = _threshold(system_section.get("memory_warning_percent", 80), "memory_warning_percent", "system")
    memory_critical = _threshold(system_section.get("memory_critical_percent", 95), "memory_critical_percent", "system")
    if cpu_warning >= cpu_critical or memory_warning >= memory_critical:
        raise ConfigError("system warning thresholds must be less than critical thresholds")
    top_processes = system_section.get("top_processes", 5)
    if isinstance(top_processes, bool) or not isinstance(top_processes, int) or not 1 <= top_processes <= 10:
        raise ConfigError("system.top_processes must be an integer from 1 to 10")
    system = SystemConfig(cpu_warning, cpu_critical, memory_warning, memory_critical, top_processes)

    network_section = raw.get("network", {})
    if not isinstance(network_section, dict):
        raise ConfigError("network must be a table")
    ping_host = network_section.get("ping_host", "")
    if not isinstance(ping_host, str):
        raise ConfigError("network.ping_host must be a host name or IP address")
    ping_host = ping_host.strip() or None
    if ping_host is not None:
        try:
            ipaddress.ip_address(ping_host)
        except ValueError:
            labels = ping_host.split(".")
            if len(ping_host) > 253 or any(
                not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label, re.ASCII)
                for label in labels
            ):
                raise ConfigError("network.ping_host must be a valid host name or IP address") from None
    ping_timeout = network_section.get("ping_timeout_seconds", 3)
    if isinstance(ping_timeout, bool) or not isinstance(ping_timeout, int) or not 1 <= ping_timeout <= 10:
        raise ConfigError("network.ping_timeout_seconds must be an integer from 1 to 10")
    external_ip_url = network_section.get("external_ip_url", "")
    if not isinstance(external_ip_url, str):
        raise ConfigError("network.external_ip_url must be a URL")
    external_ip_url = external_ip_url.strip() or None
    if external_ip_url is not None:
        try:
            ip_url = urlsplit(external_ip_url)
            ip_url.port
        except ValueError:
            raise ConfigError("Invalid external IP URL") from None
        if ip_url.scheme != "https" or not ip_url.hostname or ip_url.username or ip_url.password or ip_url.fragment:
            raise ConfigError("network.external_ip_url must use HTTPS without credentials or fragments")
    network = NetworkConfig(ping_host, ping_timeout, external_ip_url)

    logs_section = raw.get("logs", {})
    if not isinstance(logs_section, dict):
        raise ConfigError("logs must be a table")
    allowed_values = logs_section.get("allowed_directories", [])
    if not isinstance(allowed_values, list) or any(not isinstance(value, str) or not value.strip() for value in allowed_values):
        raise ConfigError("logs.allowed_directories must be a list of absolute paths")
    log_directories = []
    for value in allowed_values:
        directory = Path(value)
        if not directory.is_absolute():
            raise ConfigError("logs.allowed_directories must contain only absolute paths")
        log_directories.append(directory.resolve())
    log_entries = logs_section.get("files", [])
    if not isinstance(log_entries, list):
        raise ConfigError("logs.files must be a list of tables")
    log_name_pattern = re.compile(r"[A-Za-z0-9_.-]{1,40}\Z", re.ASCII)
    log_files = []
    for index, entry in enumerate(log_entries):
        if not isinstance(entry, dict):
            raise ConfigError(f"logs.files[{index}] must be a table")
        name, value = entry.get("name"), entry.get("path")
        if not isinstance(name, str) or not log_name_pattern.fullmatch(name):
            raise ConfigError(f"logs.files[{index}].name must be a safe alias")
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"logs.files[{index}].path must be a non-empty absolute path")
        log_path = Path(value)
        if not log_path.is_absolute():
            raise ConfigError(f"logs.files[{index}].path must be absolute")
        resolved_path = log_path.resolve()
        if not log_directories or not any(resolved_path.is_relative_to(directory) for directory in log_directories):
            raise ConfigError(f"logs.files[{index}].path must be inside logs.allowed_directories")
        log_files.append(LogFileConfig(name, resolved_path))
    if len({entry.name for entry in log_files}) != len(log_files):
        raise ConfigError("Log file names must be unique")
    max_log_lines = logs_section.get("max_lines", 200)
    if isinstance(max_log_lines, bool) or not isinstance(max_log_lines, int) or not 1 <= max_log_lines <= 500:
        raise ConfigError("logs.max_lines must be an integer from 1 to 500")

    commands_section = raw.get("commands", {})
    if not isinstance(commands_section, dict):
        raise ConfigError("commands must be a table")
    command_rate_limit = commands_section.get("rate_limit_seconds", 10)
    if isinstance(command_rate_limit, bool) or not isinstance(command_rate_limit, int) or not 1 <= command_rate_limit <= 3600:
        raise ConfigError("commands.rate_limit_seconds must be an integer from 1 to 3600")

    digest_section = raw.get("digest", {})
    if not isinstance(digest_section, dict):
        raise ConfigError("digest must be a table")
    digest_hours = digest_section.get("interval_hours", 0)
    if isinstance(digest_hours, bool) or not isinstance(digest_hours, int) or not 0 <= digest_hours <= 168:
        raise ConfigError("digest.interval_hours must be 0 (disabled) or an integer from 1 to 168")
    digest_hour = digest_section.get("hour", 8)
    if isinstance(digest_hour, bool) or not isinstance(digest_hour, int) or not 0 <= digest_hour <= 23:
        raise ConfigError("digest.hour must be an integer from 0 to 23")

    snapshot_section = raw.get("host_snapshot", {})
    if not isinstance(snapshot_section, dict):
        raise ConfigError("host_snapshot must be a table")
    snapshot_path = snapshot_section.get("path")
    if snapshot_path is not None and (not isinstance(snapshot_path, str) or not snapshot_path.startswith("/")):
        raise ConfigError("host_snapshot.path must be an absolute path")

    return AppConfig(
        disks=tuple(mounts), temperatures=tuple(temperatures), http_checks=tuple(services),
        check_interval_seconds=interval, state_file=state_path, backups=tuple(backups),
        batteries=tuple(batteries), processes=tuple(processes), system=system,
        network=network, log_files=tuple(log_files), log_directories=tuple(log_directories),
        max_log_lines=max_log_lines, command_rate_limit_seconds=command_rate_limit,
        digest_interval_hours=digest_hours, digest_hour=digest_hour,
        host_snapshot=Path(snapshot_path) if snapshot_path is not None else None,
    )
