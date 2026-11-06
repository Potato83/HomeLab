import os
import shutil
from dataclasses import dataclass


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
