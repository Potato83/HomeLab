"""Online SQLite backups without opening Store or changing application state."""

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import math
import os
from pathlib import Path
import sqlite3
import tempfile
import time

from .locking import database_path


class BackupError(RuntimeError):
    pass


def backup_database(source, output, timeout=30):
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('Таймаут резервного копирования должен быть больше нуля')
    source = database_path(source)
    target = Path(output).absolute()
    if not source.is_file():
        raise BackupError('Файл базы не найден. Проверьте BOTWORK_DB; после doctor база истории ещё не создаётся.')
    reserved = {source, *(Path(str(source) + suffix) for suffix in
                         ('-wal', '-shm', '-journal', '.lock', '.instance.lock'))}
    if target.resolve() in reserved:
        raise BackupError('Нельзя сохранить резервную копию поверх рабочей базы или её служебных файлов')
    # lexists also rejects a dangling symlink. os.link below enforces this atomically.
    if os.path.lexists(target):
        raise FileExistsError('Файл назначения уже существует; задайте новое имя резервной копии')
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=5)) as src:
        tables = {row[0] for row in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'vacancies', 'source_status', 'state'} <= tables:
            raise BackupError('В исходном файле нет основных таблиц BotWork')
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.botwork-backup-', dir=target.parent) as directory:
            temporary = Path(directory) / 'snapshot.sqlite3'
            deadline = time.monotonic() + timeout
            def progress(status, remaining, total):
                if time.monotonic() > deadline:
                    raise BackupError('Превышено время резервного копирования; попробуйте позже')
            with closing(sqlite3.connect(str(temporary))) as dst:
                src.backup(dst, pages=128, progress=progress, sleep=0.1)
                # The published backup must be a standalone file, not a file + WAL pair.
                if dst.execute('PRAGMA journal_mode=DELETE').fetchone()[0] != 'delete':
                    raise BackupError('Не удалось подготовить самостоятельный файл резервной копии')
                if dst.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                    raise BackupError('Резервная копия не прошла проверку целостности')
            temporary.chmod(0o600)
            with temporary.open('r+b') as handle:
                os.fsync(handle.fileno())
                handle.seek(0)
                checksum = hashlib.file_digest(handle, 'sha256').hexdigest()
            size = temporary.stat().st_size
            # Atomic publication without overwriting any existing target, even in a race.
            os.link(temporary, target)
    return {'ok': True, 'path': str(target), 'bytes': size, 'sha256': checksum,
            'created_at': datetime.now(timezone.utc).isoformat(timespec='seconds')}
