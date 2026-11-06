from pathlib import Path

import pytest

from serverbot.collectors.temperature import find_hwmon_by_name, get_temperature
from serverbot.config import TemperatureCheckConfig


@pytest.fixture
def check():
    return TemperatureCheckConfig("CPU", "hwmon", "k10temp", "temp1", 70, 85)


def make_hwmon(base, index, driver, value=None):
    path = base / f"hwmon{index}"
    path.mkdir()
    (path / "name").write_text(driver + "\n")
    if value is not None:
        (path / "temp1_input").write_text(value + "\n")
    return path


@pytest.mark.parametrize("index", [2, 5])
def test_temperature_found_by_driver_not_index(tmp_path, check, index):
    make_hwmon(tmp_path, 0, "nvme", "37900")
    expected = make_hwmon(tmp_path, index, "k10temp", "34600")
    assert find_hwmon_by_name("k10temp", tmp_path) == expected
    reading = get_temperature(check, tmp_path)
    assert reading.value == pytest.approx(34.6)
    assert reading.error is None


def test_missing_driver(tmp_path, check):
    reading = get_temperature(check, tmp_path)
    assert reading.value is None
    assert "Driver k10temp not found" in reading.error


def test_missing_sensor(tmp_path, check):
    make_hwmon(tmp_path, 2, "k10temp")
    reading = get_temperature(check, tmp_path)
    assert reading.value is None
    assert "Sensor temp1 not found" in reading.error


@pytest.mark.parametrize("value", ["", "bad", "nan"])
def test_invalid_temperature_value(tmp_path, check, value):
    make_hwmon(tmp_path, 2, "k10temp", value)
    reading = get_temperature(check, tmp_path)
    assert reading.value is None
    assert "Invalid reading" in reading.error


def test_temperature_permission_error(tmp_path, check, monkeypatch):
    path = make_hwmon(tmp_path, 2, "k10temp", "34600")
    original = Path.read_text

    def read_text(file, *args, **kwargs):
        if file == path / "temp1_input":
            raise PermissionError("Denied")
        return original(file, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", read_text)
    reading = get_temperature(check, tmp_path)
    assert reading.value is None
    assert "Denied" in reading.error


def test_missing_hwmon_directory(tmp_path, check):
    reading = get_temperature(check, tmp_path / "missing")
    assert reading.value is None
    assert reading.error is not None


def test_hwmon_symlink_is_followed(tmp_path, check):
    devices = tmp_path / "devices"
    devices.mkdir()
    device = make_hwmon(devices, 2, "k10temp", "34600")
    base = tmp_path / "hwmon"
    base.mkdir()
    (base / "hwmon5").symlink_to(device, target_is_directory=True)
    reading = get_temperature(check, base)
    assert reading.value == pytest.approx(34.6)
