#!/usr/bin/python3
"""Daily Alpine checks and guarded upgrades of a small set of userland packages."""
import fcntl
import json
import re
import subprocess
import time
from pathlib import Path
from probe import BASE, publish

ALLOW = {'curl', 'curl-fish-completion', 'libcurl', 'nano', 'nano-syntax',
         'libarchive', 'libexpat', 'brotli-libs', 'cups-libs', 'perl'}

def execute(args, timeout=180):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                          env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LC_ALL':'C'})

def planned_packages(text):
    return re.findall(r'^\(\s*\d+/\d+\) (?:Upgrading|Installing|Downgrading|Reinstalling) (\S+) ', text, re.M)

def main():
    status, detail = 'error', 'Update task failed'
    try:
        with (BASE/'updates.lock').open('w') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                return
            repos = [s.strip() for s in Path('/etc/apk/repositories').read_text().splitlines() if s.strip() and not s.lstrip().startswith('#')]
            if not repos or any('/v3.24/' not in r for r in repos):
                raise RuntimeError('Auto-upgrade disabled: unexpected Alpine repository branch')
            refreshed = execute(['/sbin/apk','update'])
            if refreshed.returncode:
                raise RuntimeError('Package index refresh failed')
            pending = execute(['/sbin/apk','version','-l','<'])
            if pending.returncode:
                raise RuntimeError('Cannot inspect package updates')
            # Select by exact apk info package names, never by shell interpolation.
            installed = execute(['/sbin/apk','info']).stdout.splitlines()
            candidates = sorted(ALLOW.intersection(installed))
            plan = execute(['/sbin/apk','upgrade','--preupgrade=no','--simulate',*candidates])
            changes = planned_packages(plan.stdout)
            applied = []
            reason = ''
            if not candidates or plan.returncode or any(p not in ALLOW for p in changes) or re.search(r'\) (?:Installing|Downgrading|Removing|Purging) ', plan.stdout):
                reason = 'automatic transaction deferred: protected dependencies'
            elif changes:
                cache = BASE/'apk-rollback'
                cache.mkdir(mode=0o700,exist_ok=True)
                for package in changes:
                    current = execute(['/sbin/apk','list','--installed',package])
                    lines = [s.split()[0] for s in current.stdout.splitlines() if s.startswith(package+'-') and '[installed]' in s]
                    if current.returncode or len(lines)!=1:
                        reason = 'automatic transaction deferred: cannot identify installed version'
                        break
                    version = lines[0][len(package)+1:]
                    old = execute(['/sbin/apk','fetch','--output',str(cache),f'{package}={version}'])
                    if old.returncode:
                        reason = 'automatic transaction deferred: old APK unavailable for rollback'
                        break
                if not reason:
                    result = execute(['/sbin/apk','upgrade','--preupgrade=no',*candidates],timeout=600)
                    if result.returncode:
                        raise RuntimeError('APK upgrade failed; inspect /var/log/homelab-updates.log')
                    applied = changes
            remaining = execute(['/sbin/apk','version','-l','<'])
            if remaining.returncode:
                raise RuntimeError('Cannot inspect updates after maintenance')
            count = sum(' < ' in line for line in remaining.stdout.splitlines())
            status = 'warning' if count else 'ok'
            detail = f'{count} package updates pending; automatic upgrades: {len(applied)}'
            if reason:
                detail += '; '+reason
            publish(BASE/'updates.json',dict(timestamp=time.time(),status=status,detail=detail))
            print(detail,flush=True)
            return
    except (OSError,ValueError,subprocess.TimeoutExpired,RuntimeError) as exc:
        detail = str(exc) if isinstance(exc,RuntimeError) else 'Maintenance I/O or timeout failure'
    publish(BASE/'updates.json',dict(timestamp=time.time(),status=status,detail=detail))
    print(detail,flush=True)
    raise SystemExit(1)

if __name__ == '__main__':
    main()
