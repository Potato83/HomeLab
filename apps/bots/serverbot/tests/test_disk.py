import json
import os
import runpy
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from serverbot.collectors.disk import GetDiskUsage, read_directory_usage


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


def test_directory_scan_and_snapshot_agree(tmp_path, monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / 'deploy/host'
    monkeypatch.syspath_prepend(str(scripts))
    collect_usage = runpy.run_path(str(scripts / 'disk_usage.py'))['collect_usage']
    root = tmp_path / 'disk'
    for name, byte_count in [('/srv/repo', 64 * 1024), ('/var/lib/containers', 256 * 1024)]:
        folder = root / name.lstrip('/')
        folder.mkdir(parents=True)
        (folder / 'data').write_bytes(os.urandom(byte_count))
    (root / 'srv' / 'repo' / 'copy').hardlink_to(root / 'srv' / 'repo' / 'data')
    (root / 'srv' / 'repo' / 'external').symlink_to(tmp_path, target_is_directory=True)
    weird = root / os.fsdecode(b'srv\n\xff')
    weird.mkdir()
    (weird / 'data').write_bytes(os.urandom(512 * 1024))
    mounted = root / 'mounted'
    mounted.mkdir()
    (mounted / 'data').write_bytes(os.urandom(1024 * 1024))
    mounts = collect_usage.__globals__['mount_points']()
    monkeypatch.setitem(collect_usage.__globals__, 'mount_points', lambda: mounts | {str(mounted)})

    path = tmp_path / 'disk-usage.json'
    payload = collect_usage(root)
    path.write_text(json.dumps(payload))
    groups = read_directory_usage(path)
    assert list(groups['/'].children) == ['/var', '/srv']
    file_size = (root / 'srv' / 'repo' / 'data').stat().st_blocks * 512
    folder_size = (root / 'srv' / 'repo').stat().st_blocks * 512
    link_size = (root / 'srv' / 'repo' / 'external').lstat().st_blocks * 512
    assert groups['/srv'].children['/srv/repo'] == file_size + folder_size + link_size
    assert groups['/var/lib'].children['/var/lib/containers'] >= 256 * 1024
    assert 512 * 1024 <= groups['/'].total - sum(groups['/'].children.values()) < 1024 * 1024

    payload['timestamp'] = time.time() - 4 * 3600
    path.write_text(json.dumps(payload))
    assert read_directory_usage(path) == {}
