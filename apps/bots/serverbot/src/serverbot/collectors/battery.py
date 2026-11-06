from dataclasses import dataclass
from pathlib import Path

from serverbot.config import BatteryCheckConfig

POWER_SUPPLY_PATH = Path("/sys/class/power_supply")
CHARGER_TYPES = {"Mains", "USB", "USB_C", "USB_PD", "USB_PD_DRP", "Wireless"}


@dataclass(frozen=True)
class BatteryReading:
    percentage: int | None
    battery_status: str | None
    charger_online: bool | None
    battery_error: str | None = None
    charger_error: str | None = None


def _supply_type(path: Path) -> str | None:
    try:
        return (path / "type").read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        return None


def _find_battery(check: BatteryCheckConfig, base_path: Path) -> Path:
    if check.battery_supply != "auto":
        path = base_path / check.battery_supply
        if _supply_type(path) != "Battery":
            raise OSError("Configured battery supply was not found")
        return path

    candidates = sorted(path for path in base_path.iterdir() if _supply_type(path) == "Battery")
    if len(candidates) != 1:
        raise OSError("Automatic battery detection requires exactly one battery")
    return candidates[0]


def _charger_online(check: BatteryCheckConfig, base_path: Path) -> bool:
    if check.charger_supply != "auto":
        candidates = [base_path / check.charger_supply]
    else:
        candidates = sorted(
            path for path in base_path.iterdir()
            if _supply_type(path) in CHARGER_TYPES and (path / "online").is_file()
        )
    if not candidates:
        raise OSError("No charger status is available")

    readings = []
    for path in candidates:
        if check.charger_supply != "auto" and _supply_type(path) not in CHARGER_TYPES:
            raise OSError("Configured charger supply was not found")
        try:
            value = (path / "online").read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            continue
        if value in ("0", "1"):
            readings.append(value == "1")

    if not readings:
        raise OSError("Cannot read charger status")
    return any(readings)


def get_battery_reading(
    check: BatteryCheckConfig,
    base_path: str | Path = POWER_SUPPLY_PATH,
) -> BatteryReading:
    base = Path(base_path)
    percentage = None
    battery_status = None
    battery_error = None
    charger_online = None
    charger_error = None

    try:
        battery = _find_battery(check, base)
        raw_percentage = (battery / "capacity").read_text(encoding="ascii").strip()
        percentage = int(raw_percentage)
        if not 0 <= percentage <= 100:
            raise ValueError("Battery capacity is outside 0–100")
        try:
            battery_status = (battery / "status").read_text(encoding="ascii").strip() or None
        except (OSError, UnicodeError):
            battery_status = None
    except (OSError, UnicodeError, ValueError) as error:
        battery_error = str(error) or "Cannot read battery capacity"

    try:
        charger_online = _charger_online(check, base)
    except OSError as error:
        charger_error = str(error) or "Cannot read charger status"

    return BatteryReading(percentage, battery_status, charger_online, battery_error, charger_error)
