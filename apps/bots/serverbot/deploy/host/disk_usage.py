#!/usr/bin/python3
"""Publish allocated directory sizes on the root filesystem."""
import fcntl
import os
import re
import signal
import stat
import time
from collections import defaultdict
from pathlib import Path

from probe import BASE, publish

GROUP_LIMITS = {'/': 5, '/srv': 3, '/var/lib': 3}


def ignore_disappeared(error: OSError) -> None:
    if not isinstance(error, FileNotFoundError):
        raise error


def mount_points() -> set[str]:
    paths = set()
    with open('/proc/self/mountinfo', 'rb') as info:
        for line in info:
            path = line.split(b' ', 5)[4]
            path = re.sub(rb'\\([0-7]{3})', lambda match: bytes([int(match[1], 8)]), path)
            paths.add(os.fsdecode(path))
    return paths


def collect_usage(root: Path) -> dict:
    device = root.stat().st_dev
    mounts = mount_points()
    sizes = defaultdict(int)
    hardlinks = set()
    for directory, folders, files, fd in os.fwalk(root, onerror=ignore_disappeared):
        info = os.fstat(fd)
        if info.st_dev != device:
            folders.clear()
            continue
        blocks = info.st_blocks
        subfolders = []
        for name in folders + files:
            if os.path.join(directory, name) in mounts:
                continue
            try:
                entry = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if entry.st_dev != device:
                continue
            if stat.S_ISDIR(entry.st_mode):
                subfolders.append(name)
                continue
            if entry.st_nlink > 1:
                if entry.st_ino in hardlinks:
                    continue
                hardlinks.add(entry.st_ino)
            blocks += entry.st_blocks
        folders[:] = subfolders
        parts = Path(directory).relative_to(root).parts[:3]
        for depth in range(len(parts) + 1):
            sizes['/' + '/'.join(parts[:depth])] += blocks * 512

    if '/' not in sizes:
        raise ValueError('Directory usage scan returned no root size')

    groups = {}
    for parent, limit in GROUP_LIMITS.items():
        if not sizes.get(parent):
            continue
        children = [(path, size) for path, size in sizes.items()
                    if path != '/' and (path.rpartition('/')[0] or '/') == parent
                    and len(path) <= 100 and path.isprintable()]
        children.sort(key=lambda item: (-item[1], item[0]))
        groups[parent] = {'total': sizes[parent], 'children': dict(children[:limit])}
    return {'timestamp': time.time(), 'groups': groups}


def main() -> None:
    with (BASE / 'disk-usage.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        signal.alarm(120)
        publish(BASE / 'disk-usage.json', collect_usage(Path('/')))


if __name__ == '__main__':
    main()
