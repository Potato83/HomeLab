"""Read bounded, non-secret host readings produced by a root-owned scheduled task."""
import json
import time
from dataclasses import dataclass
from pathlib import Path

@dataclass(frozen=True)
class HostReading:
    key: str
    name: str
    status: str
    detail: str


def read_host_snapshot(path: Path, max_age: int = 180) -> list[HostReading]:
    try:
        with path.open('rb') as stream:
            data = stream.read(131073)
        if len(data) > 131072:
            raise ValueError('oversized snapshot')
        payload = json.loads(data)
        timestamp = payload['timestamp']
        if type(timestamp) not in (int, float) or not 0 <= time.time() - timestamp <= max_age:
            raise ValueError('stale snapshot')
        checks = payload['checks']
        if not isinstance(checks, list) or len(checks) > 64:
            raise ValueError('invalid checks')
        result = []
        for item in checks:
            if (not isinstance(item, dict) or set(item) != {'key', 'name', 'status', 'detail'}
                    or any(not isinstance(item[k], str) for k in item)
                    or item['status'] not in {'ok', 'warning', 'critical', 'error'}
                    or len(item['key']) > 100 or len(item['name']) > 100 or len(item['detail']) > 1000):
                raise ValueError('invalid reading')
            result.append(HostReading(**item))
        if not result or len({r.key for r in result}) != len(result):
            raise ValueError('empty or duplicate readings')
        return result
    except (OSError, ValueError, KeyError, TypeError):
        return [HostReading('host:collector', 'Host collector', 'error', 'Snapshot missing, invalid or stale')]
