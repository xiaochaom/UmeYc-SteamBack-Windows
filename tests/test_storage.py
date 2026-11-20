from pathlib import Path
import shutil
import sys

import pytest

from umeyc_steamback.backup_storage import migrate_backups
from umeyc_steamback.desktop import app_directory
from umeyc_steamback.engine import Config, Engine, WindowsSniffer
from umeyc_steamback.storage import initialize_storage, migrate_legacy
from tests.test_sync import setup


def test_application_directory_uses_executable_not_working_directory_or_bundle(tmp_path, monkeypatch):
    exe = tmp_path / "portable/UmeYc-SteamBack-Windows.exe"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "temporary-bundle"), raising=False)
    monkeypatch.chdir(tmp_path)
    assert app_directory() == exe.parent.resolve()
    from umeyc_steamback.cli import data_directory
    assert data_directory() == exe.parent.resolve() / "data"


def test_legacy_state_and_all_accounts_move_to_portable_tree(tmp_path):
    old, app = tmp_path / "AppData/old", tmp_path / "portable"
    contents = {"games.json": b'{"123:42": {}}', "quark/session.cookies": b"encrypted-DPAPI",
                "upload_queue.json": b'{"pending": {}}', "steam_config_backups/example.vdf": b"steam",
                "backups/123/42/one.zip": b"zip one", "backups/123/42/one.json": b"record one",
                "backups/456/43/two.zip": b"zip two", "backups/456/43/two.json": b"record two"}
    for relative, payload in contents.items():
        path = old / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    initialize_storage(app / "data", app / "backups", previous=old)
    assert not old.exists()
    for relative, payload in contents.items():
        path = app / relative if relative.startswith("backups/") else app / "data" / relative
        assert path.read_bytes() == payload
    assert not (app / "data/backups").exists()
    # A migrated install does not import obsolete settings a second time.
    (app / "data/games.json").write_bytes(b"new configuration")
    old.mkdir()
    (old / "games.json").write_bytes(b"stale configuration")
    initialize_storage(app / "data", app / "backups", previous=old)
    assert (app / "data/games.json").read_bytes() == b"new configuration"


def test_legacy_running_version_blocks_migration(tmp_path):
    from PySide6.QtCore import QLockFile
    old = tmp_path / "old"
    old.mkdir()
    (old / "games.json").write_text("{}")
    lock = QLockFile(str(old / ".application.lock"))
    assert lock.tryLock(0)
    try:
        with pytest.raises(ValueError, match="旧版工具仍在运行"):
            initialize_storage(tmp_path / "app/data", tmp_path / "app/backups", previous=old)
        assert (old / "games.json").exists()
    finally:
        lock.unlock()


def test_migration_conflict_preserves_both_copies(tmp_path):
    old, app = tmp_path / "old", tmp_path / "app"
    old.mkdir()
    (app / "data").mkdir(parents=True)
    (old / "games.json").write_bytes(b"old configuration")
    (app / "data/games.json").write_bytes(b"new configuration")
    with pytest.raises(ValueError, match="同名冲突"):
        migrate_legacy(old, app / "data", app / "backups")
    assert (old / "games.json").read_bytes() == b"old configuration"
    assert (app / "data/games.json").read_bytes() == b"new configuration"


def test_interrupted_copy_keeps_originals_and_can_resume(tmp_path, monkeypatch):
    old, dest = tmp_path / "old", tmp_path / "dest"
    old.mkdir()
    (old / "one.zip").write_bytes(b"archive")
    (old / "one.json").write_bytes(b"metadata")
    real_copy = shutil.copy2
    def fail(source, target):
        if Path(source).suffix == ".json":
            raise PermissionError("disk unavailable")
        return real_copy(source, target)
    with monkeypatch.context() as m:
        m.setattr(shutil, "copy2", fail)
        with pytest.raises(PermissionError):
            migrate_backups(old, dest)
    assert (old / "one.zip").read_bytes() == b"archive"
    assert (old / "one.json").read_bytes() == b"metadata"
    assert (dest / "one.zip").read_bytes() == b"archive"
    assert not (dest / "one.json").exists()
    assert not list(dest.glob(".migrate-*"))
    migrate_backups(old, dest)
    assert not old.exists()
    assert (dest / "one.json").read_bytes() == b"metadata"


def test_corrupt_copy_does_not_publish_or_remove_original(tmp_path, monkeypatch):
    old, dest = tmp_path / "old", tmp_path / "dest"
    old.mkdir()
    (old / "one.zip").write_bytes(b"archive")
    def corrupt(source, target):
        Path(target).write_bytes(b"bad copy")
    monkeypatch.setattr(shutil, "copy2", corrupt)
    with pytest.raises(OSError, match="校验失败"):
        migrate_backups(old, dest)
    assert (old / "one.zip").read_bytes() == b"archive"
    assert not list(dest.iterdir())


def test_existing_backups_migrate_and_restore_from_new_location(setup, tmp_path):
    e, game, source, client, cloud = setup
    record = e.backup(game)
    e.rename(game, record["id"], "通关前")
    old = e.backup_dir(game)
    e.config.backup_root = str(tmp_path / "portable/backups")
    assert e.backups(game)[0]["name"] == "通关前"
    assert not old.exists()
    (source / "slot.sav").write_bytes(b"new progress")
    e.restore(game, record["id"])
    assert (source / "slot.sav").read_bytes() == b"chapter one"
    assert e.backup_dir(game) == tmp_path / "portable/backups/123/42"
    e.delete(game, record["id"])
    assert record["id"] not in {r["id"] for r in e.backups(game)}
    assert (source / "slot.sav").read_bytes() == b"chapter one"


def test_configured_backup_folder_cannot_be_a_save_source(setup, tmp_path):
    e, game, source, client, cloud = setup
    e.config.backup_root = str(tmp_path / "portable/backups")
    nested = Path(e.config.backup_root) / "123/42"
    nested.mkdir(parents=True)
    for path in [nested, Path(e.config.backup_root), tmp_path / "portable"]:
        with pytest.raises(ValueError, match="不能相互包含"):
            e.configure(game, [str(path)])


def test_external_backups_are_not_sniffed(setup, tmp_path):
    e, game, source, client, cloud = setup
    e.config.backup_root = str(tmp_path / "portable/backups")
    folder = Path(e.config.backup_root)
    folder.mkdir(parents=True)
    (folder / "copy.sav").write_bytes(b"backup")
    sniff = WindowsSniffer(e)
    for roots in [[tmp_path / "portable"], [folder], [folder / "nested"]]:
        found, _ = sniff.scan(roots)
        assert not found


def test_cloud_records_and_safety_copies_use_portable_backup_root(setup, tmp_path):
    e, game, source, client, cloud = setup
    e.config.backup_root = str(tmp_path / "portable/backups")
    cloud.upload(123, game)
    (source / "slot.sav").write_bytes(b"local progress")
    cloud.download_current(123, game)
    assert {r["source"] for r in e.backups(game)} == {"cloud_download", "before_cloud_sync"}
    assert len(list((tmp_path / "portable/backups/123/42").glob("*.zip"))) == 2
    assert not (e.data / "backups").exists()


def test_unwritable_backup_destination_never_falls_back(setup, tmp_path, monkeypatch):
    e, game, source, client, cloud = setup
    e.config.backup_root = str(tmp_path / "portable/backups")
    original = Path.mkdir
    def deny(path, *args, **kwargs):
        if path == Path(e.config.backup_root) or Path(e.config.backup_root) in path.parents:
            raise PermissionError("read-only app directory")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "mkdir", deny)
    with pytest.raises(PermissionError):
        e.backup(game)
    assert not (e.data / "backups").exists()
