"""Verified, restartable migration of local backups to the portable app folder."""
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
import threading


_MIGRATION_LOCK = threading.RLock()


def _digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _linked(path):
    return path.is_symlink() or (path.exists() and bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400))


def migrate_files(pairs):
    """Publish ZIPs before metadata; keep originals until all copies verify.

    Existing identical files allow retry after interruption. A different file
    with the same name blocks migration rather than overwriting either copy.
    The application holds its process lock; this lock also covers UI/workers.
    """
    with _MIGRATION_LOCK:
        pairs = sorted(pairs, key=lambda pair: (pair[0].suffix == ".json", str(pair[0])))
        for source, target in pairs:
            if (not source.is_file() or any(_linked(p) for p in (source, *source.parents))
                    or any(_linked(p) for p in (target, *target.parents))):
                raise ValueError("数据迁移遇到无效文件，原文件已保留")
            if target.exists() and (not target.is_file() or _digest(source) != _digest(target)):
                raise ValueError(f"数据迁移遇到同名冲突，双方文件已保留：{target}")
        for source, target in pairs:
            if target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, pending = tempfile.mkstemp(dir=target.parent, prefix=".migrate-")
            os.close(fd)
            try:
                shutil.copy2(source, pending)
                if _digest(source) != _digest(pending):
                    raise OSError("备份迁移校验失败，原备份已保留")
                os.replace(pending, target)
            finally:
                Path(pending).unlink(missing_ok=True)
        # No deletion on a copy error. Delete metadata first so a restart
        # never sees an old record whose archive has already been removed.
        for source, _ in reversed(pairs):
            source.unlink()


def migrate_backups(previous, destination):
    previous, destination = Path(previous), Path(destination)
    with _MIGRATION_LOCK:
        if previous.resolve() == destination.resolve() or not previous.exists():
            return
        pairs = [(p, destination / p.name) for p in previous.iterdir() if p.suffix in (".zip", ".json")]
        migrate_files(pairs)
        try:
            previous.rmdir()
        except OSError:
            pass  # Leave unrelated files alone.
