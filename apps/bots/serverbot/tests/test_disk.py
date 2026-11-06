from types import SimpleNamespace

import pytest

from serverbot.collectors.disk import GetDiskUsage


def test_disk_usage_normal_result(tmp_path, monkeypatch):
    monkeypatch.setattr("serverbot.collectors.disk.shutil.disk_usage", lambda path: SimpleNamespace(total=1000, used=801, free=199))
    reading = GetDiskUsage(str(tmp_path))
    assert (reading.total, reading.used, reading.free) == (1000, 801, 199)
    assert reading.percent == 80.1
    assert reading.path == str(tmp_path)


def test_missing_disk_path(tmp_path):
    with pytest.raises(FileNotFoundError):
        GetDiskUsage(str(tmp_path / "missing"))


@pytest.mark.parametrize("error", [PermissionError("Denied"), OSError("Unavailable")])
def test_disk_system_error(tmp_path, monkeypatch, error):
    def disk_usage(path):
        raise error
    monkeypatch.setattr("serverbot.collectors.disk.shutil.disk_usage", disk_usage)
    with pytest.raises(type(error)):
        GetDiskUsage(str(tmp_path))
