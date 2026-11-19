#!/usr/bin/python3
"""Replace OmniRoute under OpenRC, with recoverable rollback."""

import argparse
import fcntl
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

import control


SERVICE = 'omniroute'
NEXT = 'omniroute-update-next'
OLD = 'omniroute-update-old'
REPO = 'docker.io/diegosouzapw/omniroute'
LATEST = REPO + ':latest'
PREVIOUS_IMAGE = 'localhost/homelab/omniroute:previous'
BASE = Path('/srv/omniroute')
DATA = BASE / 'data'
STATE = BASE / '.update'
PENDING = STATE / 'pending.json'
SNAPSHOT = STATE / 'snapshot'
COPYING = STATE / 'copying'
PREVIOUS = STATE / 'previous'
FAILED_DATA = STATE / 'failed-data'
RETAINED = STATE / 'previous-image'
PULLED = STATE / 'last-pulled-image'
PULL_STARTED = STATE / 'pull-started'
REJECTED = STATE / 'rejected-image'
MAINTENANCE = control.BASE / 'omniroute-maintenance'
START_PERMIT = Path('/run/homelab-omniroute-start')
PODMAN = '/usr/bin/podman'
IMAGE_ID = re.compile(r'[0-9a-f]{64}')
MIN_FREE_BYTES = 8 * 1024**3
LOCK_FDS = ()


class UpdateError(Exception):
    pass


def command(args, timeout=20, valid=(0,)):
    descriptors = ','.join(map(str, LOCK_FDS))
    wrapper = [sys.executable, '-B', __file__, '_locked-command', descriptors, str(timeout)]
    with tempfile.TemporaryFile() as stream:
        result = subprocess.run(
            [*wrapper, *args], pass_fds=LOCK_FDS, env=control.ENV, cwd='/',
            stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.DEVNULL,
        )
        status = result.returncode
        stream.seek(max(0, stream.tell() - 131072))
        output = stream.read().decode('utf-8', errors='replace')
    if status == 124:
        raise UpdateError(f'{Path(args[0]).name} {args[1]} timed out')
    if status not in valid:
        raise UpdateError(f'{Path(args[0]).name} {args[1]} failed ({status})')
    return status, output.strip()


def locked_command(descriptors, timeout, *args):
    # Hold both locks while the command runs even if the updater is killed.
    # Do not pass them on to any daemons created by the command.
    for descriptor in descriptors.split(','):
        if descriptor:
            os.set_inheritable(int(descriptor), False)
    try:
        return subprocess.run(args, timeout=float(timeout)).returncode
    except subprocess.TimeoutExpired:
        return 124


def podman(*args, **kwargs):
    return command([PODMAN, *args], **kwargs)[1]


def image_id(value):
    value = value.removeprefix('sha256:')
    if not IMAGE_ID.fullmatch(value):
        raise UpdateError('unexpected image ID')
    return value


def container(name):
    status, _ = command([PODMAN, 'container', 'exists', name], valid=(0, 1))
    if status == 1:
        return None
    return tuple(podman(
        'container', 'inspect', '--format', '{{.ID}} {{.Image}} {{.State.Running}}', name,
    ).split())


def image(reference):
    status, _ = command([PODMAN, 'image', 'exists', reference], valid=(0, 1))
    if status == 1:
        return None
    return json.loads(podman('image', 'inspect', reference))[0]


def immutable_reference(details):
    prefix = REPO + '@sha256:'
    preferred = REPO + '@' + (details.get('Digest') or '')
    digests = [ref for ref in (details.get('RepoDigests') or []) if ref.startswith(prefix)
               and IMAGE_ID.fullmatch(ref.removeprefix(prefix))]
    if not digests:
        raise UpdateError('pulled image has no repository digest')
    return preferred if preferred in digests else digests[0]


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def durable_rename(source, destination):
    source.rename(destination)
    sync_dir(destination.parent)
    if source.parent != destination.parent:
        sync_dir(source.parent)


def put(path, value):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w') as stream:
        stream.write(value + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    durable_rename(temporary, path)


def remove(path):
    if path.exists():
        path.unlink()
        sync_dir(path.parent)


def saved_image(path):
    return image_id(path.read_text().strip()) if path.exists() else None


def save(record, phase):
    record['phase'] = phase
    put(PENDING, json.dumps(record))


def prune_image(identifier):
    if not identifier:
        return
    details = image(identifier)
    if details is not None and not details['RepoTags']:
        # --no-prune limits the removal to this known image. In-use images are refused.
        status, _ = command([PODMAN, 'image', 'rm', '--no-prune', identifier], valid=(0, 2))
        if status:
            print(f'kept image {identifier[:12]} (in use)', flush=True)


def require_free_space():
    if shutil.disk_usage(BASE).free < MIN_FREE_BYTES:
        raise UpdateError('less than 8 GiB free on the OmniRoute filesystem')


def pull():
    old = saved_image(PULLED)
    before = image(LATEST)
    before_id = image_id(before['Id']) if before is not None else None
    if PULL_STARTED.exists():
        if before_id is not None and before_id not in (old, PULL_STARTED.read_text().strip()):
            prune_image(old)
            put(PULLED, before_id)
            old = before_id
        remove(PULL_STARTED)
    put(PULL_STARTED, before_id or '-')
    podman('pull', '--quiet', LATEST, timeout=900)
    details = image(LATEST)
    if details is None:
        raise UpdateError('pulled image is unavailable')
    identifier = image_id(details['Id'])
    if before_id != identifier:
        if old != identifier:
            prune_image(old)
        put(PULLED, identifier)
    remove(PULL_STARTED)
    return identifier, immutable_reference(details)


def create(reference):
    podman(
        'create', '--name', NEXT, '--pull=never', '--network', 'host',
        '--memory', '2g', '--memory-swap', '2g', '--cpus', '2', '--pids-limit', '128',
        '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
        '--restart', 'on-failure:5', '--stop-timeout', '40',
        '--log-driver', 'k8s-file', '--log-opt', 'max-size=10485760',
        '--env-file', str(BASE / '.env'), '-v', f'{DATA}:/app/data', reference,
    )


def stop():
    command(['/sbin/rc-service', SERVICE, 'stop'], timeout=60)
    current = container(SERVICE)
    if current is not None and current[2] != 'false':
        raise UpdateError('container did not stop')


def start():
    START_PERMIT.touch()
    try:
        command(['/sbin/rc-service', SERVICE, 'start'], timeout=55)
    finally:
        START_PERMIT.unlink(missing_ok=True)


def endpoint_status(port, path):
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
    try:
        connection.request('GET', path)
        with connection.getresponse() as response:
            return response.status
    except (OSError, http.client.HTTPException):
        return None
    finally:
        connection.close()


def wait_ready(seconds=45):
    deadline = time.monotonic() + seconds
    while True:
        health = endpoint_status(20127, '/api/monitoring/health')
        api = endpoint_status(20128, '/v1/models')
        if health == 200 and api == 401 and control.observed_state(SERVICE) == 'running':
            return
        if time.monotonic() >= deadline:
            raise UpdateError(f'readiness failed (health={health}, API={api})')
        time.sleep(2)


def snapshot():
    if not DATA.is_dir() or DATA.is_symlink():
        raise UpdateError('unexpected data directory')
    command(['/bin/cp', '-a', str(DATA), str(COPYING)], timeout=85)
    command(['/bin/sync', '-f', str(COPYING)], timeout=45)
    durable_rename(COPYING, SNAPSHOT)


def restore_snapshot():
    if SNAPSHOT.exists():
        if DATA.exists():
            if FAILED_DATA.exists():
                raise UpdateError('unexpected rollback directory')
            durable_rename(DATA, FAILED_DATA)
        durable_rename(SNAPSHOT, DATA)
    elif not FAILED_DATA.exists() or not DATA.is_dir():
        raise UpdateError('rollback snapshot is missing')


def finish(record, leave_stopped=False):
    if MAINTENANCE.exists():
        current = container(SERVICE)
        if current is None or current[0] != record['active_container']:
            raise UpdateError('finished container disappeared')
        if not leave_stopped:
            if current[2] != 'true':
                start()
            wait_ready()
        remove(MAINTENANCE)
    for directory in (COPYING, SNAPSHOT, FAILED_DATA):
        if directory.exists():
            shutil.rmtree(directory)
    sync_dir(STATE)
    prune_image(record.get('retired_image'))
    remove(PENDING)


def rollback(record, leave_stopped=False):
    old = container(OLD)
    current = container(SERVICE)
    if old is not None:
        if old[0] != record['old_container'] or old[2] != 'false' or not MAINTENANCE.exists():
            raise UpdateError('unexpected rollback container state')
        if current is not None:
            if image_id(current[1]) != record['candidate']:
                raise UpdateError('unexpected live container during rollback')
            if current[2] == 'true':
                stop()
            podman('rm', SERVICE)
        restore_snapshot()
        podman('rename', OLD, SERVICE)
    elif current is None or current[0] != record['old_container']:
        raise UpdateError('original container disappeared')

    candidate = container(NEXT)
    if candidate is not None:
        if image_id(candidate[1]) != record['candidate'] or candidate[2] != 'false':
            raise UpdateError('unexpected staged container during rollback')
        podman('rm', NEXT)

    had_maintenance = MAINTENANCE.exists()
    record['active_container'] = record['old_container']
    save(record, 'finished')
    finish(record, leave_stopped)
    if had_maintenance:
        print('restored previous OmniRoute', flush=True)


def commit(record, leave_stopped=False, recheck=False):
    current = container(SERVICE)
    if current is None or image_id(current[1]) != record['candidate']:
        raise UpdateError('validated container disappeared')
    previous = image(PREVIOUS_IMAGE)
    tagged = image_id(previous['Id']) if previous is not None else None
    if tagged not in (None, record['old_previous'], record['old_image']):
        raise UpdateError('previous image tag was changed')
    if recheck and MAINTENANCE.exists() and not leave_stopped:
        try:
            if current[2] != 'true':
                start()
            wait_ready()
        except (UpdateError, OSError):
            if tagged == record['old_previous'] and SNAPSHOT.exists() and container(OLD):
                put(REJECTED, record['candidate'])
                save(record, 'trying')
                rollback(record)
            raise
    if tagged != record['old_image']:
        if tagged:
            podman('image', 'untag', tagged, PREVIOUS_IMAGE)
        podman('image', 'tag', record['old_image'], PREVIOUS_IMAGE)
    put(RETAINED, record['old_image'])

    if SNAPSHOT.exists():
        if PREVIOUS.exists():
            shutil.rmtree(PREVIOUS)
        durable_rename(SNAPSHOT, PREVIOUS)
    elif not PREVIOUS.is_dir():
        raise UpdateError('validated data backup disappeared')

    old = container(OLD)
    if old is not None:
        if old[0] != record['old_container'] or old[2] != 'false':
            raise UpdateError('unexpected previous container state')
        podman('rm', OLD)
    if record['old_previous'] != record['old_image']:
        record['retired_image'] = record['old_previous']
    record['active_container'] = current[0]
    save(record, 'finished')
    finish(record, leave_stopped)
    print(f'updated OmniRoute to image {record["candidate"][:12]}', flush=True)


def recover(leave_stopped=False):
    record = json.loads(PENDING.read_text())
    if record['phase'] == 'finished':
        finish(record, leave_stopped)
    elif record['phase'] == 'committing':
        commit(record, leave_stopped, recheck=True)
    elif record['phase'] == 'validated':
        try:
            current = container(SERVICE)
            if current is None or image_id(current[1]) != record['candidate']:
                raise UpdateError('validated container disappeared')
            if not leave_stopped:
                if current[2] != 'true':
                    start()
                wait_ready()
        except (UpdateError, OSError):
            put(REJECTED, record['candidate'])
            save(record, 'trying')
            rollback(record, leave_stopped)
            raise
        save(record, 'committing')
        commit(record, leave_stopped)
    elif record['phase'] == 'trying':
        rollback(record, leave_stopped)
    else:
        raise UpdateError('unexpected update phase')


def update(active, identifier, reference):
    require_free_space()
    if any(path.exists() for path in (MAINTENANCE, SNAPSHOT, COPYING, FAILED_DATA)):
        raise UpdateError('unowned maintenance or staging path already exists')
    if container(NEXT) is not None or container(OLD) is not None:
        raise UpdateError('unowned staging container already exists')
    retained = saved_image(RETAINED)
    previous = image(PREVIOUS_IMAGE)
    if (image_id(previous['Id']) if previous is not None else None) != retained:
        raise UpdateError('unowned previous image tag already exists')
    wait_ready(seconds=0)
    record = dict(candidate=identifier, old_container=active[0],
                  old_image=image_id(active[1]), old_previous=retained)
    save(record, 'trying')
    try:
        create(reference)
        control.save_intent(SERVICE, 'busy')
        put(MAINTENANCE, 'updating')
        stop()
        control.save_intent(SERVICE, 'busy')
        snapshot()
        control.save_intent(SERVICE, 'busy')
        podman('rename', SERVICE, OLD)
        podman('rename', NEXT, SERVICE)
        try:
            start()
            wait_ready()
        except (UpdateError, OSError):
            put(REJECTED, identifier)
            raise
    except (UpdateError, OSError):
        rollback(record)
        raise
    save(record, 'validated')
    save(record, 'committing')
    commit(record)


@contextmanager
def lock(path):
    global LOCK_FDS
    with path.open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        previous = LOCK_FDS
        LOCK_FDS = (*previous, stream.fileno())
        try:
            yield True
        finally:
            LOCK_FDS = previous


def run(recover_only):
    if recover_only and not PENDING.exists():
        return
    STATE.mkdir(mode=0o700, exist_ok=True)
    if STATE.is_symlink() or STATE.stat().st_uid != 0 or STATE.stat().st_mode & 0o077:
        raise UpdateError('update directory is not root-private')
    with lock(STATE / 'update.lock') as updating:
        if not updating:
            return
        recovering = PENDING.exists()
        if not recovering:
            if recover_only or control.observed_state(SERVICE) != 'running':
                return
            require_free_space()
            identifier, reference = pull()
            if saved_image(REJECTED) == identifier:
                return
        with lock(control.BASE / 'omniroute.lock') as controlling:
            if not controlling:
                return
            maintained = False
            try:
                if recovering:
                    START_PERMIT.unlink(missing_ok=True)
                    leave_stopped = (control.read_intent(SERVICE).get('state') == 'stopped'
                                     and control.observed_state(SERVICE) == 'stopped')
                    maintained = MAINTENANCE.exists() and not leave_stopped
                    recover(leave_stopped)
                elif control.observed_state(SERVICE) == 'running':
                    active = container(SERVICE)
                    if active is not None and active[2] == 'true' and image_id(active[1]) != identifier:
                        maintained = True
                        update(active, identifier, reference)
            finally:
                if maintained and control.read_intent(SERVICE).get('state') == 'busy':
                    control.save_intent(SERVICE, None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--recover', action='store_true', help='recover a pending update only')
    args = parser.parse_args()
    if os.geteuid():
        parser.error('must run as root')
    try:
        run(args.recover)
    except UpdateError as error:
        print(f'OmniRoute update: {error}', file=sys.stderr)
        return 1
    except (OSError, ValueError, TypeError, KeyError, IndexError) as error:
        print(f'OmniRoute update: {type(error).__name__}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    if (sys.argv[1:2] == ['_locked-command'] and not os.geteuid() and len(sys.argv) >= 5
            and sys.argv[4] in (PODMAN, '/bin/cp', '/bin/sync', '/sbin/rc-service')):
        sys.exit(locked_command(*sys.argv[2:]))
    sys.exit(main())
