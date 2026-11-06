from contextlib import contextmanager
from pathlib import Path


class InstanceRunning(RuntimeError):
    pass


def database_path(value):
    if not value or str(value) == ':memory:':
        raise ValueError('Для постоянной работы нужен путь к файлу SQLite')
    # Resolve symlinks and relative aliases so they share the same lock.
    return Path(value).resolve()


@contextmanager
def file_lock(path, message, error_type=RuntimeError):
    import fcntl
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Keep the file after unlock. Removing it can create two independently locked inodes.
    with path.open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise error_type(message) from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def instance_lock(value):
    path = database_path(value)
    return file_lock(str(path) + '.instance.lock',
        'Другой экземпляр уже использует эту базу. Остановите его; для внеплановой проверки работающего бота используйте /check.',
        InstanceRunning)
