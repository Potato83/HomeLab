import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DiskUsageInfo:
    path: str
    total: int
    used: int
    free: int
    percent: float

# Get disk usage
def GetDiskUsage(path: str) -> DiskUsageInfo:

    if not os.path.exists(path):
        raise FileNotFoundError(f"Path '{path}' does not exist")

    try:
        usage = shutil.disk_usage(path)

        if usage.total > 0:
            percent = round((usage.used / usage.total) * 100, 2)
        else:
            percent = 0.0

        return DiskUsageInfo(
            path=path,
            total=usage.total,
            used=usage.used,
            free=usage.free,
            percent=percent
        )

    except PermissionError as e:
        raise PermissionError(f"No access rights to the path '{path}': {e}")
    except OSError as e:
        raise OSError(f"Error retrieving disk data for '{path}': {e}")


@dataclass(frozen=True)
class DirectoryUsage:
    total: int
    children: dict[str, int]


def read_directory_usage(path: Path, max_age: int = 3 * 3600) -> dict[str, DirectoryUsage]:
    try:
        with path.open('rb') as stream:
            data = stream.read(8193)
        if len(data) > 8192:
            return {}
        payload = json.loads(data)
        stamp = payload['timestamp']
        if type(stamp) not in (int, float) or not 0 <= time.time() - stamp <= max_age:
            return {}
        groups = payload['groups']
        if not isinstance(groups, dict) or '/' not in groups or groups.keys() - {'/', '/srv', '/var/lib'}:
            return {}
        result = {}
        for parent, group in groups.items():
            total, children = group['total'], group['children']
            if (type(total) is not int or not 0 < total < 2**64
                    or not isinstance(children, dict) or len(children) > 5):
                return {}
            for name, size in children.items():
                directory, _, basename = name.rpartition('/')
                if (not name.startswith('/') or (directory or '/') != parent
                        or basename in {'', '.', '..'} or len(name) > 100 or not name.isprintable()
                        or type(size) is not int or not 0 <= size <= total):
                    return {}
            result[parent] = DirectoryUsage(total, children)
        return result
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        return {}
