"""All persistent app state lives beside the executable; migrate older installs."""
import os
from pathlib import Path

from .backup_storage import migrate_files, _linked
from .desktop import app_directory


def data_directory():
    return app_directory() / "data"


def legacy_directory():
    if os.name == "nt":
        return Path(os.environ["LOCALAPPDATA"]) / "UmeYc-SteamBack-Windows"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "UmeYc-SteamBack-Windows"


def migrate_legacy(previous, data, backups):
    """Caller owns both process locks. Complete migration before loading state."""
    previous, data, backups = Path(previous), Path(data), Path(backups)
    marker = data / ".legacy-migrated"
    if previous.resolve() == data.resolve() or not previous.exists() or marker.exists():
        return
    pairs = []
    directories = []
    def scan_error(error):
        raise error
    for directory, dirs, files in os.walk(previous, followlinks=False, onerror=scan_error):
        parent = Path(directory)
        if any(_linked(p) for p in (parent, *(parent / d for d in dirs))):
            raise ValueError("旧数据包含目录链接，请先检查旧数据目录；原文件已保留")
        directories.append(parent)
        for name in files:
            source = parent / name
            relative = source.relative_to(previous)
            if relative == Path(".application.lock"):
                continue
            if relative.parts[0] == "backups":
                target = backups / Path(*relative.parts[1:])
            else:
                target = data / relative
            pairs.append((source, target))
    migrate_files(pairs)
    data.mkdir(parents=True, exist_ok=True)
    marker.write_text("migrated\n", encoding="utf-8")
    for directory in reversed(directories):
        try:
            directory.rmdir()
        except OSError:
            pass  # The process lock is still held until the caller returns.


def initialize_storage(data, backups, previous=None):
    """Must run under the new data lock; exclude any running old version."""
    from PySide6.QtCore import QLockFile
    previous = Path(previous) if previous is not None else legacy_directory()
    if not previous.exists() or (Path(data) / ".legacy-migrated").exists():
        return
    legacy_lock = QLockFile(str(previous / ".application.lock"))
    if not legacy_lock.tryLock(100):
        raise ValueError("旧版工具仍在运行，请先退出旧版再迁移数据")
    try:
        migrate_legacy(previous, data, backups)
    finally:
        legacy_lock.unlock()
        try:
            previous.rmdir()
        except OSError:
            pass
