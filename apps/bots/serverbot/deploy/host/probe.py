#!/usr/bin/python3
"""Fixed read-only checks. Run by root; publish only non-secret health metrics."""
import fcntl
import json
import os
import socket
import subprocess
import time
from pathlib import Path

BASE = Path('/var/lib/homelab-health')
KALI_SSH_HOST = os.environ.get('HOMELAB_KALI_SSH_HOST', '').strip()

def run(args, timeout=8, user=None, cwd=None):
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, user=user, group=user, extra_groups=() if user else None, cwd=cwd, env={
        'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C', 'PYTHONDONTWRITEBYTECODE': '1'})
    return p.returncode, p.stdout

def publish(path, data):
    tmp = path.with_suffix('.tmp')
    with tmp.open('w') as stream:
        json.dump(data, stream)
        stream.flush()
        os.fsync(stream.fileno())
    tmp.chmod(0o644)
    tmp.replace(path)

def is_bot_process(args, user, module):
    script = f'/srv/bots/{user}/.venv/bin/bot'.encode()
    return script in args or any(
        arg == b'-m' and following == module.encode()
        for arg, following in zip(args, args[1:])
    )

def main():
    with (BASE/'probe.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        try:
            old = json.loads((BASE/'host.json').read_text())
        except (OSError, ValueError):
            old = {}
        checks, streaks = [], {}
        def add(key, name, status, detail, immediate=False):
            streaks[key] = old.get('streaks', {}).get(key, 0) + 1 if status != 'ok' else 0
            if status != 'ok' and not immediate and streaks[key] < 2:
                status, detail = 'ok', 'Waiting for confirmation of first failed sample'
            checks.append(dict(key=key, name=name, status=status, detail=detail))
        def check(key, name, fn):
            try:
                status, detail = fn()
                add(key, name, status, detail)
            except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
                add(key, name, 'error', 'Health query failed')
        for service in ('serverbot','chatbot','workbot','sshd','nftables','libvirtd','sing-box','chronyd','crond','omniroute','nginx'):
            check('service:'+service, service, lambda s=service: (
                ('ok', 'OpenRC running') if run(['/sbin/rc-service', s, 'status'])[0] == 0
                else ('critical', 'OpenRC not running')))
        for user, module in (('serverbot','serverbot.main'),('chatbot','chatbot.main'),('workbot','workbot')):
            def process(user=user, module=module):
                import pwd
                uid = pwd.getpwnam(user).pw_uid
                matches = 0
                for p in Path('/proc').iterdir():
                    try:
                        if p.name.isdigit() and p.stat().st_uid == uid:
                            args = (p/'cmdline').read_bytes().split(b'\0')
                            if is_bot_process(args, user, module):
                                matches += 1
                    except (OSError, IndexError):
                        continue
                return ('ok' if matches == 1 else 'critical', f'Expected one app process; found {matches}')
            check('bot:'+user, user+' process', process)
        check('bot:chatbot-db', 'ChatBot database', lambda: ('ok','Database schema ready') if run([
            '/srv/bots/chatbot/.venv/bin/python','-m','chatbot.healthcheck'],
            user='chatbot',cwd='/srv/bots/chatbot')[0] == 0 else ('critical','Database healthcheck failed'))
        def workbot_db():
            code, text = run(['/usr/bin/python3','-c',
                'import sqlite3; c=sqlite3.connect("file:/srv/bots/workbot/data/workbot.sqlite3?mode=ro",uri=True,timeout=3); '
                'print(c.execute("SELECT COUNT(*) FROM source_status WHERE error IS NOT NULL").fetchone()[0]); c.close()'],
                user='workbot',cwd='/srv/bots/workbot')
            if code:
                return 'critical','Database query failed'
            errors = int(text.strip())
            return ('warning' if errors else 'ok',f'Source errors: {errors}; database readable')
        check('bot:workbot-db','WorkBot sources',workbot_db)
        check('vm:kali', 'Kali VM', lambda: ('ok','running') if run([
            '/usr/bin/virsh','-c','qemu:///system','domstate','kali-ctf'])[1].strip() == 'running'
            else ('critical','VM is not running'))
        def kali_ssh():
            if not KALI_SSH_HOST:
                return ('warning','HOMELAB_KALI_SSH_HOST is not configured')
            with socket.create_connection((KALI_SSH_HOST,22), timeout=4) as conn:
                return ('ok','SSH responds') if conn.recv(128).startswith(b'SSH-') else ('critical','Unexpected SSH banner')
        check('vm:ssh', 'Kali SSH', kali_ssh)
        check('network:dns', 'Host DNS', lambda: ('ok','Resolver responds') if run([
            '/usr/bin/getent','hosts','dl-cdn.alpinelinux.org'])[0] == 0 else ('critical','DNS lookup failed'))
        def https():
            import urllib.request
            with urllib.request.urlopen('https://dl-cdn.alpinelinux.org/alpine/', timeout=5) as response:
                return ('ok','Alpine HTTPS endpoint responds') if response.status == 200 else ('warning','Unexpected HTTP status')
        check('network:https','Host internet HTTPS',https)
        def gateway():
            import urllib.request
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open('http://127.0.0.1:20127/api/monitoring/health',timeout=5) as response:
                return ('ok','OmniRoute health endpoint responds') if response.status == 200 else ('critical','Unexpected gateway response')
        check('gateway:health','OmniRoute API',gateway)
        check('time:ntp','Clock synchronization',lambda: ('ok','chrony synchronized') if
            'Leap status     : Normal' in run(['/usr/bin/chronyc','tracking'])[1] else ('warning','chrony not synchronized'))
        def smart():
            code, text = run(['/usr/sbin/smartctl','-a','-j','/dev/nvme0n1'])
            data = json.loads(text)
            health = data['nvme_smart_health_information_log']
            warning, errors, used = health['critical_warning'], health['media_errors'], health['percentage_used']
            status = 'critical' if warning or errors else 'warning' if used >= 80 else 'ok'
            return status, f'Warning={warning}; media errors={errors}; wear={used}%; unsafe shutdowns={health["unsafe_shutdowns"]}'
        check('disk:smart','NVMe SMART',smart)
        stat = os.statvfs('/')
        inode = 100*(1-stat.f_favail/stat.f_files) if stat.f_files else 0
        add('disk:inodes','Root inode usage','critical' if inode>=95 else 'warning' if inode>=85 else 'ok',f'{inode:.1f}% used')
        mem = {line.split(':')[0]:int(line.split()[1]) for line in Path('/proc/meminfo').read_text().splitlines()}
        swap = 100*(1-mem['SwapFree']/mem['SwapTotal']) if mem['SwapTotal'] else 0
        add('memory:swap','Swap usage','critical' if swap>=80 else 'warning' if swap>=50 else 'ok',f'{swap:.1f}% used')
        try:
            updates = json.loads((BASE/'updates.json').read_text())
            age = time.time()-updates['timestamp']
            add('system:updates','System updates', 'error' if age>129600 else updates['status'], updates['detail'])
        except (OSError,ValueError,KeyError):
            add('system:updates','System updates','warning','Update task has not published a result')
        publish(BASE/'host.json', dict(timestamp=time.time(),checks=checks,streaks=streaks))

if __name__ == '__main__':
    main()
