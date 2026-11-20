import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
from unittest.mock import patch
from zipfile import ZipFile, ZipInfo

import pytest

from umeyc_steamback.engine import Config, Engine, WindowsSniffer
from umeyc_steamback.protocol.cloud import CloudConflict, pack_current, unpack_verified, digest
from umeyc_steamback.protocol.custom import safe_file
from umeyc_steamback.sync import WindowsCloud
from tests.fake_quark import MemoryQuark


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr("umeyc_steamback.steam.running_games", lambda _: set())
    source = tmp_path / "游戏存档"
    source.mkdir()
    (source / "slot.sav").write_bytes(b"chapter one")
    os.utime(source / "slot.sav", ns=(1700000000000000000, 1700000000000000000))
    engine = Engine(Config(str(tmp_path / "data"), str(tmp_path / "Steam")))
    engine.account_ids = {123}
    game = {"game_id": 42, "game_name": "测试游戏", "install_root": str(tmp_path / "Steam"), "install_dir": "Test"}
    engine.configure(game, [str(source)])
    client = MemoryQuark()
    cloud = WindowsCloud(engine, client)
    return engine, game, source, client, cloud


def test_cloud_roundtrip_protects_local_files_and_undo_removes_new_files(setup):
    e, g, source, client, cloud = setup
    (source / "new.sav").write_bytes(b"cloud new")
    cloud.upload(123, g)
    remote = cloud.latest(123, g)
    (source / "slot.sav").write_bytes(b"local changed")
    (source / "new.sav").unlink()
    (source / "extra.sav").write_bytes(b"preserve")
    cloud.download(123, g, remote["revision"], client.user_id)
    assert (source / "slot.sav").read_bytes() == b"chapter one"
    assert (source / "extra.sav").read_bytes() == b"preserve"
    assert (source / "new.sav").read_bytes() == b"cloud new"
    backups = e.backups(g)
    assert {b["source"] for b in backups} == {"cloud_download", "before_cloud_sync"}
    undo = next(b for b in backups if b["source"] == "before_cloud_sync")
    e.restore(g, undo["id"])
    assert (source / "slot.sav").read_bytes() == b"local changed"
    assert not (source / "new.sav").exists()


def test_custom_and_auto_remote_mapping_roundtrip(setup, tmp_path):
    e, g, source, client, cloud = setup
    keys = ["proton/drive_c/users/steamuser/AppData/Local/Test/Saved"]
    e.configure(g, [str(source)], keys, "auto")
    cloud.upload(123, g)
    inspect = cloud.inspect(123, g)
    dest = tmp_path / "Windows AppData"
    dest.mkdir()
    e.configure(g, [str(dest)], keys, "auto")
    cloud.download(123, g, inspect["remote"]["revision"], client.user_id)
    assert (dest / "slot.sav").read_bytes() == b"chapter one"
    archive = tmp_path / "windows.zip"
    manifest = pack_current(e, g, archive)
    assert manifest["mode"] == "auto"
    assert manifest["root_keys"] == keys
    assert manifest["content_hash"] == inspect["manifest"]["content_hash"]


def test_upload_retains_one_pair_and_preserves_unknown_files(setup):
    e, g, source, client, cloud = setup
    cloud.upload(123, g)
    folder = cloud.directory(123, g)
    unknown = client.upload(source / "slot.sav", folder, "玩家文件.txt")
    cloud.upload(123, g)
    for i in range(3):
        (source / "slot.sav").write_text(f"chapter {i}")
        cloud.upload(123, g)
    files = client.list_files(folder)
    assert sum(f["file_name"].endswith(".json") for f in files) == 1
    assert sum(f["file_name"].endswith(".zip") for f in files) == 1
    assert unknown["fid"] in client.items
    assert not e.backups(g)


def test_failed_publish_preserves_old_cloud(setup):
    e, g, source, client, cloud = setup
    cloud.upload(123, g)
    remote = cloud.latest(123, g)
    client.fail_commit = True
    (source / "slot.sav").write_bytes(b"changed")
    with pytest.raises(ValueError):
        cloud.upload(123, g)
    assert cloud.latest(123, g)["revision"] == remote["revision"]
    assert remote["archive_fid"] in client.items


def test_conflict_requires_revision_and_revision_race_is_rejected(setup):
    e, g, source, client, cloud = setup
    cloud.upload(123, g)
    remote = cloud.latest(123, g)
    cloud.state_path.unlink()
    (source / "slot.sav").write_bytes(b"changed")
    with pytest.raises(CloudConflict):
        cloud.upload(123, g)
    with pytest.raises(CloudConflict):
        cloud.upload(123, g, "obsolete")
    cloud.upload(123, g, remote["revision"])
    assert cloud.latest(123, g)["revision"] != remote["revision"]


def test_download_rejects_changed_revision_or_account(setup):
    e, g, source, client, cloud = setup
    cloud.upload(123, g)
    remote = cloud.latest(123, g)
    with pytest.raises(ValueError):
        cloud.download(123, g, "old", client.user_id)
    with pytest.raises(ValueError):
        cloud.download(123, g, remote["revision"], "other account")


def test_local_backup_rename_delete_and_uninstall_do_not_touch_live_save(setup):
    e, g, source, client, cloud = setup
    record = e.backup(g)
    e.rename(g, record["id"], "通关之前")
    assert e.backups(g)[0]["name"] == "通关之前"
    e2 = Engine(e.config)
    e2.account_ids = {123}
    assert e2.backups(g)[0]["id"] == record["id"]
    e.delete(g, record["id"])
    assert not e.backups(g)
    assert (source / "slot.sav").read_bytes() == b"chapter one"


@pytest.mark.parametrize("name", ["../a", "/tmp/a", "C:/a", "C:a", "a\\b", "a:b", "CON", "NUL.txt", "com1.sav", "a.", "a ", ".", "", "a//b", "a/./b"])
def test_windows_unsafe_paths_rejected(tmp_path, name):
    with pytest.raises(ValueError):
        safe_file(tmp_path, name)


def test_corrupt_archive_never_overwrites(setup):
    e, g, source, client, cloud = setup
    cloud.upload(123, g)
    remote = cloud.latest(123, g)
    client.payloads[remote["archive_fid"]] += b"corrupted"
    (source / "slot.sav").write_bytes(b"local")
    with pytest.raises(ValueError, match="校验"):
        cloud.download(123, g, remote["revision"], client.user_id)
    assert (source / "slot.sav").read_bytes() == b"local"


def test_running_game_blocks_restore_backup_upload_and_publish(setup, monkeypatch):
    e, g, source, client, cloud = setup
    record = e.backup(g)
    monkeypatch.setattr("umeyc_steamback.steam.running_games", lambda _: {42})
    for operation in [lambda: e.backup(g), lambda: e.restore(g, record["id"]), lambda: cloud.upload(123, g)]:
        with pytest.raises(ValueError, match="正在运行"):
            operation()


def test_restore_failure_rolls_back_all_written_files(setup, monkeypatch):
    e, g, source, client, cloud = setup
    (source / "z.sav").write_bytes(b"cloud z")
    cloud.upload(123, g)
    remote = cloud.latest(123, g)
    (source / "slot.sav").write_bytes(b"local a")
    (source / "z.sav").write_bytes(b"local z")
    real = os.replace
    def fail(src, dst):
        if Path(dst) == source / "z.sav":
            raise OSError("injected disk error")
        return real(src, dst)
    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        cloud.download(123, g, remote["revision"], client.user_id)
    assert (source / "slot.sav").read_bytes() == b"local a"
    assert (source / "z.sav").read_bytes() == b"local z"


def test_sniff_only_suggests_changed_files_and_excludes_data(setup):
    e, g, source, client, cloud = setup
    sniff = WindowsSniffer(e)
    with patch.object(sniff, "roots", return_value=[source]):
        sniff.begin(g, 123)
        (source / "slot.sav").write_bytes(b"new progress")
        (source / "cache.log").write_text("ignore")
        found = sniff.finish(g, 123)
    assert [r["path"] for r in found["candidates"]] == [str(source / "slot.sav")]
    assert e.settings(g)["paths"] == [str(source)]


def test_archive_duplicate_and_case_collisions_rejected(setup, tmp_path):
    e, g, source, client, cloud = setup
    zip_path = tmp_path / "bad.zip"
    manifest = pack_current(e, g, zip_path)
    with ZipFile(zip_path, "a") as z:
        z.writestr("files/0/slot.sav", b"evil")
    with pytest.raises(ValueError, match="重复"):
        unpack_verified(zip_path, tmp_path / "staging", g, 123, manifest["content_hash"])


def test_cloud_mapping_mismatch_preserves_live_files(setup, tmp_path):
    e, g, source, client, cloud = setup
    cloud.upload(123, g)
    remote = cloud.latest(123, g)
    e.configure(g, [str(source)], ["proton/drive_c/another"], "auto")
    (source / "slot.sav").write_bytes(b"keep local")
    with pytest.raises(ValueError, match="映射"):
        cloud.download(123, g, remote["revision"], client.user_id)
    assert (source / "slot.sav").read_bytes() == b"keep local"


def test_initial_download_into_empty_folder_can_be_undone(setup, tmp_path):
    e, g, source, client, cloud = setup
    cloud.upload(123, g)
    remote = cloud.latest(123, g)
    empty = tmp_path / "empty"
    empty.mkdir()
    e.configure(g, [str(empty)])
    cloud.download(123, g, remote["revision"], client.user_id)
    before = next(b for b in e.backups(g) if b["source"] == "before_cloud_sync")
    assert before["empty"]
    e.restore(g, before["id"])
    assert not (empty / "slot.sav").exists()
    assert any(b["source"] == "before_restore" for b in e.backups(g))
