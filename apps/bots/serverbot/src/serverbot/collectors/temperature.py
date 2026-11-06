import re
from dataclasses import dataclass
from pathlib import Path

from serverbot.config import TemperatureCheckConfig


@dataclass(frozen=True)
class TemperatureReading:
    name: str
    value: float | None
    error: str | None


# Find driver by name, hwmon indexes can change
def find_hwmon_by_name(driver: str, base_path: str | Path = "/sys/class/hwmon") -> Path | None:
    for hwmon_path in sorted(Path(base_path).iterdir()):
        if not hwmon_path.name.startswith("hwmon") or not hwmon_path.is_dir():
            continue
        try:
            name = (hwmon_path / "name").read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            continue
        if name == driver:
            return hwmon_path
    return None


# Read temperature in millidegrees Celsius
def read_hwmon_sensor(hwmon_path: Path, sensor: str) -> float:
    if not re.fullmatch(r"temp[1-9][0-9]*", sensor):
        raise ValueError("Sensor must look like temp1 or temp2")
    sensor_path = hwmon_path / f"{sensor}_input"
    try:
        value = sensor_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as e:
        raise FileNotFoundError(f"Sensor {sensor} not found: {sensor_path}") from e
    try:
        return int(value) / 1000
    except ValueError as e:
        raise ValueError(f"Invalid reading from sensor {sensor}") from e


def get_temperature(check: TemperatureCheckConfig, base_path: str | Path = "/sys/class/hwmon",) -> TemperatureReading:
    try:
        if check.source != "hwmon":
            raise ValueError("Only source = hwmon is supported")
        hwmon_path = find_hwmon_by_name(check.driver, base_path)
        if hwmon_path is None:
            raise FileNotFoundError(f"Driver {check.driver} not found")
        value = read_hwmon_sensor(hwmon_path, check.sensor)
        return TemperatureReading(name=check.name, value=value, error=None)
    except (OSError, ValueError) as e:
        return TemperatureReading(name=check.name, value=None, error=str(e))
