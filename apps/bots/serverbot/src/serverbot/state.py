import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

STATUSES = {"ok", "warning", "critical", "error"}


def threshold_status(value: float, warning: float, critical: float) -> str:
    if value >= critical:
        return "critical"
    if value >= warning:
        return "warning"
    return "ok"


def low_threshold_status(value: float, warning: float, critical: float) -> str:
    if value <= critical:
        return "critical"
    if value <= warning:
        return "warning"
    return "ok"


def should_notify(previous: str | None, current: str) -> bool:
    # Initial healthy checks are silent; initial problems are reported once
    return previous != current and (previous is not None or current != "ok")


class StateError(ValueError):
    pass


class StateStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.states = self.load()

    def load(self) -> dict[str, dict[str, str]]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as error:
            # Do not silently reset a damaged file and send all alerts again
            raise StateError("Cannot read monitoring state") from error
        if not isinstance(raw, dict):
            raise StateError("Monitoring state must be an object")
        for user_id, checks in raw.items():
            if not user_id.isascii() or not user_id.isdigit() or int(user_id) <= 0 or not isinstance(checks, dict):
                raise StateError("Invalid recipient in monitoring state")
            for key, status in checks.items():
                if not key or not isinstance(status, str) or status not in STATUSES:
                    raise StateError("Invalid check in monitoring state")
        return raw

    def save(self, states: dict[str, dict[str, str]]) -> None:
        temporary_path = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Write beside the target so os.replace works on the same filesystem
            with NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent, prefix=".state-", delete=False) as file:
                temporary_path = Path(file.name)
                json.dump(states, file, indent=2, sort_keys=True)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_path, self.path)
        except OSError as error:
            raise StateError("Cannot save monitoring state") from error
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        # Update memory only after the complete file has been replaced
        self.states = states
