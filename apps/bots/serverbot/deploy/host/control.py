#!/usr/bin/python3 -I
"""Root-owned, fixed-command bridge for ServerBot. Never accepts paths or shell code."""
import fcntl
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BASE = Path('/var/lib/homelab-control')
BOOT_ID = Path('/proc/sys/kernel/random/boot_id')
SERVICES = ('chatbot', 'workbot', 'omniroute')
TARGETS = ('kali', *SERVICES)
ENV = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C',
       'HOME': '/root', 'PYTHONDONTWRITEBYTECODE': '1'}
STOPPED_DETAIL = 'Stopped manually'
BUSY_DETAIL = 'Control operation in progress'
CHECKS = {
    'kali': ('vm:kali', 'vm:ssh'),
    'chatbot': ('service:chatbot', 'bot:chatbot', 'bot:chatbot-db'),
    'workbot': ('service:workbot', 'bot:workbot', 'bot:workbot-db'),
    'omniroute': ('service:omniroute', 'gateway:health'),
}


class ControlError(Exception):
    pass


def run(args, timeout=8):
    # Even container log output is bounded in memory. No command output on stderr.
    with tempfile.TemporaryFile() as output:
        result = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=output,
                                stderr=output, timeout=timeout, env=ENV, cwd='/')
        size = output.tell()
        output.seek(max(0, size - 131072))
        return result.returncode, output.read().decode('utf-8', errors='replace')


def installed(target):
    return target == 'kali' or Path('/etc/init.d', target).is_file()


def observed_state(target):
    try:
        if target == 'kali':
            code, output = run(['/usr/bin/virsh', '-c', 'qemu:///system', 'domstate', 'kali-ctf'], timeout=5)
            if code:
                return 'unknown'
            return {'running': 'running', 'shut off': 'stopped'}.get(output.strip(), 'unknown')
        code, _ = run(['/sbin/rc-service', target, 'status'], timeout=5)
        return {0: 'running', 3: 'stopped'}.get(code, 'unknown')
    except (OSError, subprocess.TimeoutExpired):
        return 'unknown'


def read_intent(target):
    try:
        value = json.loads((BASE / (target + '.json')).read_text())
        if value.get('boot_id') == BOOT_ID.read_text().strip():
            return value
    except (OSError, ValueError, AttributeError):
        pass
    return {}


def save_intent(target, state):
    path = BASE / (target + '.json')
    if state is None:
        path.unlink(missing_ok=True)
        return
    value = dict(state=state, boot_id=BOOT_ID.read_text().strip(), deadline=time.time() + 180)
    # BASE is root-owned, never writable by the bot or log-producing services.
    with tempfile.NamedTemporaryFile(mode='w', dir=BASE, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
            temporary.chmod(0o644)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def monitoring_overrides():
    """Intent alone cannot hide a crash: confirm a manual stop against live state."""
    result = {}
    for target in TARGETS:
        intent = read_intent(target)
        if intent.get('state') == 'busy' and time.time() < intent.get('deadline', 0):
            result.update(dict.fromkeys(CHECKS[target], BUSY_DETAIL))
        elif intent.get('state') == 'stopped':
            state = observed_state(target)
            if state == 'stopped':
                result.update(dict.fromkeys(CHECKS[target], STOPPED_DETAIL))
            elif state == 'running':
                # A start over SSH also ends maintenance. Never hide a later crash.
                # Coordinate with execute() so reconciliation cannot erase a new operation.
                with (BASE / (target + '.lock')).open('a') as lock:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        continue
                    if read_intent(target).get('state') == 'stopped' and observed_state(target) == 'running':
                        save_intent(target, None)
    return result


def describe(target):
    state = observed_state(target)
    intent = read_intent(target)
    return dict(target=target, state=state,
                manual=state == 'stopped' and intent.get('state') == 'stopped')


def wait_for(target, expected, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if observed_state(target) == expected:
            return
        time.sleep(1)
    raise ControlError('state_timeout')


def execute(target, action):
    with (BASE / (target + '.lock')).open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ControlError('busy') from None
        before = observed_state(target)
        expected_before = 'stopped' if action == 'start' else 'running'
        if before != expected_before:
            raise ControlError('state_changed')
        previous = read_intent(target)
        save_intent(target, 'busy')
        try:
            if target == 'kali':
                # A restart is a graceful shutdown followed by start, verified at both steps.
                commands = ['shutdown', 'start'] if action == 'restart' else [
                    'shutdown' if action == 'stop' else 'start']
                for command in commands:
                    code, _ = run(['/usr/bin/virsh', '-c', 'qemu:///system', command, 'kali-ctf'], timeout=15)
                    if code:
                        raise ControlError('command_failed')
                    wait_for(target, 'stopped' if command == 'shutdown' else 'running',
                             90 if command == 'shutdown' else 30)
            else:
                code, _ = run(['/sbin/rc-service', target, action], timeout=110)
                if code:
                    raise ControlError('command_failed')
                wait_for(target, 'stopped' if action == 'stop' else 'running', 20)
            save_intent(target, 'stopped' if action == 'stop' else None)
        except (ControlError, OSError, subprocess.TimeoutExpired):
            # Keep the intended stop if a restart failed after gracefully stopping.
            stopped = observed_state(target) == 'stopped'
            save_intent(target, 'stopped' if stopped and (
                action == 'stop' or previous.get('state') == 'stopped') else None)
            raise
        return describe(target)


LEVEL = re.compile(r'\b(?:WARNING|WARN|ERROR|CRITICAL|FATAL)\b|\b(?:[A-Za-z_][\w.]*Error|Exception):', re.I)
RECORD = re.compile(r'^\s*(?:\d{4}-\d\d-\d\d[ T].*?|\[.*?)?\b(DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL|FATAL)\b', re.I)
ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')


def redact(text):
    text = ANSI.sub('', text)
    text = re.sub(r'\b\d{5,}:[A-Za-z0-9_-]{20,}\b', '[REDACTED]', text)
    text = re.sub(r'(?i)(\b(?:Bearer|Basic)\s+)\S+', r'\1[REDACTED]', text)
    text = re.sub(r'(?i)(["\']?(?:[\w-]*(?:token|password|secret|api[_-]?key|authorization)[\w-]*)["\']?\s*[:=]\s*)(?:"[^"\n]*"|\'[^\'\n]*\'|[^\s,;&]+)', r'\1[REDACTED]', text)
    text = re.sub(r'(https?://)[^/\s:@]+:[^/\s@]+@', r'\1[REDACTED]@', text)
    return ''.join(c if c.isprintable() or c in '\n\t' else '?' for c in text)


def filter_errors(text):
    blocks, current = [], []
    for line in ANSI.sub('', text).splitlines():
        record = RECORD.search(line)
        level = record.group(1).upper() if record else None
        if line.lstrip().startswith('{'):
            try:
                structured = json.loads(line)
                raw_level = structured.get('level', structured.get('severity'))
                level = {10: 'DEBUG', 20: 'DEBUG', 30: 'INFO', 40: 'WARN',
                         50: 'ERROR', 60: 'FATAL'}.get(raw_level, str(raw_level).upper())
            except (ValueError, AttributeError, TypeError):
                pass
        if level is not None:
            if current:
                blocks.append('\n'.join(current))
            current = [line] if level in {'WARNING', 'WARN', 'ERROR', 'CRITICAL', 'FATAL'} else []
        elif LEVEL.search(line) or line.startswith('Traceback (most recent call last):'):
            current.append(line)
        elif current and (line.startswith((' ', '\t', 'During handling', 'The above exception')) or not line):
            current.append(line)
        elif current:
            blocks.append('\n'.join(current))
            current = []
    if current:
        blocks.append('\n'.join(current))
    # Keep whole recent records; a huge traceback is explicitly marked as truncated.
    selected = []
    remaining = 12000
    for block in reversed(blocks[-10:]):
        block = redact(block)
        if len(block) > remaining:
            if not selected:
                selected.append('[… начало записи сокращено …]\n' + block[-remaining:])
            break
        selected.append(block)
        remaining -= len(block) + 2
    return '\n\n'.join(reversed(selected))


def read_tail(path):
    # Reject symlinks and special files even when a compromised bot controls its logs.
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise OSError('not a regular log file')
        offset = max(0, info.st_size - 131072)
        stream.seek(offset)
        data = stream.read(131072)
        if offset:
            data = data.partition(b'\n')[2]
        return data.decode('utf-8', errors='replace')


def errors(target):
    if target == 'omniroute':
        code, text = run(['/usr/bin/podman', 'logs', '--tail', '300', 'omniroute'])
        if code:
            raise ControlError('logs_unavailable')
        return filter_errors(text)
    texts = []
    missing = 0
    for name in (target + '.log', target + '-error.log'):
        try:
            texts.append(read_tail(Path('/var/log', target, name)))
        except FileNotFoundError:
            missing += 1
    if missing == 2:
        raise ControlError('logs_unavailable')
    # Deduplicate identical records mirrored into stdout and stderr.
    return filter_errors('\n'.join(dict.fromkeys(texts)))


def dispatch(args):
    if args == ['list']:
        targets = [target for target in TARGETS if installed(target)]
        with ThreadPoolExecutor(max_workers=5) as pool:
            return {'targets': list(pool.map(describe, targets))}
    if len(args) != 2 or args[0] not in TARGETS or args[1] not in {
            'status', 'errors', 'start', 'stop', 'restart'}:
        raise ControlError('invalid_action')
    target, action = args
    if not installed(target):
        raise ControlError('not_installed')
    if action == 'status':
        return describe(target)
    if action == 'errors':
        if target == 'kali':
            raise ControlError('invalid_action')
        return {'text': errors(target)}
    return execute(target, action)


def main():
    try:
        if os.geteuid() != 0:
            raise ControlError('permission_denied')
        payload = dict(ok=True, **dispatch(sys.argv[1:]))
    except ControlError as error:
        payload = dict(ok=False, error=str(error))
    except subprocess.TimeoutExpired:
        payload = dict(ok=False, error='state_timeout')
    except (OSError, ValueError, TypeError):
        payload = dict(ok=False, error='unavailable')
    print(json.dumps(payload, ensure_ascii=True))


if __name__ == '__main__':
    main()
