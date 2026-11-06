import math
import stat
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from serverbot.config import BackupCheckConfig
from serverbot.state import threshold_status


@dataclass(frozen=True)
class BackupReading:
    name: str
    status: str
    last_success_at: datetime | None
    age_hours: float | None
    error: str | None


def get_backup_reading(check: BackupCheckConfig, now: float | None = None) -> BackupReading:
    try:
        marker = check.marker_file.stat()
    except FileNotFoundError:
        return BackupReading(check.name, "error", None, None, "Marker file not found")
    except OSError:
        return BackupReading(check.name, "error", None, None, "Marker file cannot be read")

    if not stat.S_ISREG(marker.st_mode):
        return BackupReading(check.name, "error", None, None, "Marker path is not a regular file")

    current_time = time.time() if now is None else now
    age_seconds = current_time - marker.st_mtime
    if not math.isfinite(age_seconds):
        return BackupReading(check.name, "error", None, None, "Marker timestamp is invalid")
    if age_seconds < -60:
        return BackupReading(check.name, "error", None, None, "Marker timestamp is in the future")

    age_hours = max(age_seconds, 0) / 3600
    status = threshold_status(age_hours, check.warning_after_hours, check.critical_after_hours)
    try:
        last_success = datetime.fromtimestamp(marker.st_mtime, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return BackupReading(check.name, "error", None, None, "Marker timestamp is invalid")
    return BackupReading(check.name, status, last_success, age_hours, None)
