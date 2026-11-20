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
    monkeypatch.setattr("umeyc_steamback.steam.running_games", lambda _, **kwargs: set())
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


def test_exit_upload_ignores_host_registry_flag_but_still_guards_real_game(setup, monkeypatch):
    e, g, source, client, cloud = setup
    e.config.exited_wrapper_games = frozenset({42})
    real_game_running = True
    def running(items, ignore_registry_for=None):
        return {42} if real_game_running or ignore_registry_for != 42 else set()
    monkeypatch.setattr("umeyc_steamback.steam.running_games", running)
    monkeypatch.setattr("umeyc_steamback.desktop.check_steam_account", lambda _: None)
    with pytest.raises(ValueError, match="游戏正在运行"):
        cloud.upload_after_exit(123, g, client.user_id, wait=lambda _: None)
    assert not client.items
    real_game_running = False
    result = cloud.upload_after_exit(123, g, client.user_id, wait=lambda _: None)
    assert result["game_id"] == 42 and cloud.latest(123, g)


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


def test_sync_results_include_published_times_without_an_extra_cloud_read(setup):
    e, g, source, client, cloud = setup
    uploaded = cloud.upload(123, g)
    remote = cloud.latest(123, g)
    assert uploaded["remote"] == remote
    assert remote["save_time"] == 1700000000000
    assert remote["uploaded_at"] > remote["save_time"]
    # Repeating a sync must show the retained cloud time, not invent a new
    # upload time just because the button was clicked again.
    unchanged = cloud.upload(123, g)
    assert unchanged["remote"] == remote
    downloaded = cloud.download(123, g, remote["revision"], client.user_id)
    assert downloaded["remote"] == remote
    for result in (uploaded, unchanged, downloaded):
        assert (result["uid"], result["account"], result["game_id"]) == (client.user_id, 123, 42)


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


def test_manual_upload_refreshes_stale_running_cache_for_only_this_game(setup):
    e, g, source, client, cloud = setup
    cloud.running = {(123, 42), (123, 43), (456, 42)}
    snapshot = cloud.manual_snapshot(123, g)
    result = cloud.manual_transfer(123, g, "upload", snapshot)
    assert "上传" in result["message"]
    assert result["remote"]["content_hash"] == snapshot["local_hash"]
    assert cloud.running == {(123, 43), (456, 42)}


def test_live_game_still_blocks_upload_even_without_cached_state(setup, monkeypatch):
    e, g, source, client, cloud = setup
    assert not cloud.running
    monkeypatch.setattr("umeyc_steamback.steam.running_games", lambda *args, **kwargs: {42})
    with pytest.raises(ValueError, match="正在运行"):
        cloud.upload(123, g)
    assert not client.items


def test_game_restarting_during_upload_prevents_publication_after_cache_refresh(setup, monkeypatch):
    e, g, source, client, cloud = setup
    cloud.upload(123, g)
    remote = cloud.latest(123, g)
    (source / "slot.sav").write_bytes(b"new progress")
    cloud.running.add((123, 42))
    upload = client.upload
    def start_game_after_archive(*args):
        result = upload(*args)
        if str(args[2]).endswith(".zip"):
            monkeypatch.setattr("umeyc_steamback.steam.running_games", lambda *args, **kwargs: {42})
        return result
    monkeypatch.setattr(client, "upload", start_game_after_archive)
    with pytest.raises(ValueError, match="正在运行"):
        cloud.upload(123, g)
    assert cloud.latest(123, g)["revision"] == remote["revision"]


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


def test_direct_download_binds_deck_auto_keys_without_expanding_selected_files(setup):
    e, g, source, client, cloud = setup
    keys = ["proton/drive_c/users/steamuser/AppData/Local/Test/Saved"]
    second = source / "other.sav"
    second.write_bytes(b"second cloud save")
    selected = [str(source / "slot.sav"), str(second)]
    e.configure(g, selected, keys, "auto")
    cloud.upload(123, g)
    e.configure(g, selected)
    (source / "slot.sav").write_bytes(b"new local save")
    second.write_bytes(b"second local save")
    unrelated = source / "untouched.ini"
    unrelated.write_bytes(b"preserve")
    cloud.download_current(123, g)
    assert (source / "slot.sav").read_bytes() == b"chapter one"
    assert second.read_bytes() == b"second cloud save"
    assert unrelated.read_bytes() == b"preserve"
    assert e.settings(g)["paths"] == selected
    assert e.settings(g)["keys"] == keys
    assert e.settings(g)["mode"] == "auto"
    assert e._custom_plan(g)["custom_files"] == {"": ["other.sav", "slot.sav"]}


def test_direct_download_cannot_expand_single_file_to_whole_directory(setup):
    e, g, source, client, cloud = setup
    keys = ["proton/Test/Saved"]
    (source / "other.sav").write_bytes(b"cloud other")
    e.configure(g, [str(source)], keys, "auto")
    cloud.upload(123, g)
    e.configure(g, [str(source / "slot.sav")])
    (source / "slot.sav").write_bytes(b"local slot")
    (source / "other.sav").write_bytes(b"local other")
    with pytest.raises(ValueError, match="未选择"):
        cloud.download_current(123, g)
    assert (source / "slot.sav").read_bytes() == b"local slot"
    assert (source / "other.sav").read_bytes() == b"local other"
    assert e.settings(g)["paths"] == [str(source / "slot.sav")]
    assert e.settings(g)["keys"] is None
    assert {b["source"] for b in e.backups(g)} == {"cloud_download"}


def test_direct_download_requires_matching_number_of_roots(setup, tmp_path):
    e, g, source, client, cloud = setup
    second = tmp_path / "second"
    second.mkdir()
    (second / "save.dat").write_bytes(b"second save")
    e.configure(g, [str(source), str(second)], ["proton/first", "proton/second"], "auto")
    cloud.upload(123, g)
    e.configure(g, [str(source)])
    (source / "slot.sav").write_bytes(b"local progress")
    with pytest.raises(ValueError, match="数量"):
        cloud.download_current(123, g)
    assert (source / "slot.sav").read_bytes() == b"local progress"


@pytest.mark.parametrize("change", ["revision", "quark_account", "steam_account", "running"])
def test_direct_download_race_preserves_live_save_and_keeps_download(setup, monkeypatch, change):
    e, g, source, client, cloud = setup
    cloud.upload(123, g)
    remote = cloud.latest(123, g)
    (source / "slot.sav").write_bytes(b"local progress")
    real_download = client.download
    def download(fid, destination, limit=None):
        real_download(fid, destination, limit)
        if fid != remote["archive_fid"]:
            return
        if change == "revision":
            index = client.items[remote["revision"]]
            p = source.parent / "new-index.json"
            p.write_bytes(client.payloads[remote["revision"]])
            client.upload(p, index["parent"], index["file_name"])
        elif change == "quark_account":
            client.user_id = "new account"
        elif change == "steam_account":
            cloud.active_account = 456
        else:
            monkeypatch.setattr("umeyc_steamback.steam.running_games", lambda _, **kwargs: {42})
    monkeypatch.setattr(client, "download", download)
    with pytest.raises(ValueError):
        cloud.download_current(123, g)
    assert (source / "slot.sav").read_bytes() == b"local progress"
    assert {b["source"] for b in e.backups(g)} == {"cloud_download"}


@pytest.mark.parametrize("direction", ["upload", "download"])
def test_manual_transfer_uses_confirmed_snapshot(setup, direction):
    e, g, source, client, cloud = setup
    cloud.upload(123, g)
    (source / "slot.sav").write_bytes(b"local progress")
    snapshot = cloud.manual_snapshot(123, g)
    assert snapshot["local_time"] > snapshot["remote"]["save_time"]
    cloud.manual_transfer(123, g, direction, snapshot)
    if direction == "upload":
        assert cloud.latest(123, g)["content_hash"] == snapshot["local_hash"]
    else:
        assert (source / "slot.sav").read_bytes() == b"chapter one"
        assert {b["source"] for b in e.backups(g)} == {"cloud_download", "before_cloud_sync"}


@pytest.mark.parametrize("direction", ["upload", "download"])
@pytest.mark.parametrize("change", ["content", "time", "path", "account", "revision"])
def test_manual_confirmation_cannot_authorize_a_changed_save(setup, tmp_path, direction, change):
    e, g, source, client, cloud = setup
    cloud.upload(123, g)
    snapshot = cloud.manual_snapshot(123, g)
    if change == "content":
        (source / "slot.sav").write_bytes(b"new save")
    elif change == "time":
        stamp = (source / "slot.sav").stat().st_mtime_ns + 10**9
        os.utime(source / "slot.sav", ns=(stamp, stamp))
    elif change == "path":
        another = tmp_path / "another"
        shutil.copytree(source, another)
        e.configure(g, [str(another)])
    elif change == "account":
        client.user_id = "other-user"
    else:
        (source / "slot.sav").write_bytes(b"cloud change")
        cloud.upload(123, g)
        (source / "slot.sav").write_bytes(b"chapter one")
    live = (source / "slot.sav").read_bytes()
    remote = cloud.latest(123, g)["revision"]
    with pytest.raises(ValueError, match="确认期间"):
        cloud.manual_transfer(123, g, direction, snapshot)
    assert (source / "slot.sav").read_bytes() == live
    assert cloud.latest(123, g)["revision"] == remote
    assert not e.backups(g)


def test_manual_download_rechecks_local_time_after_transfer(setup, monkeypatch):
    e, g, source, client, cloud = setup
    cloud.upload(123, g)
    snapshot = cloud.manual_snapshot(123, g)
    real_download = client.download
    def download(fid, destination, limit=None):
        real_download(fid, destination, limit)
        if fid == snapshot["remote"]["archive_fid"]:
            (source / "slot.sav").write_bytes(b"new progress during download")
    monkeypatch.setattr(client, "download", download)
    with pytest.raises(ValueError, match="下载期间"):
        cloud.manual_transfer(123, g, "download", snapshot)
    assert (source / "slot.sav").read_bytes() == b"new progress during download"
    assert {b["source"] for b in e.backups(g)} == {"cloud_download"}


def test_manual_upload_checks_packaged_save_against_confirmation(setup, monkeypatch):
    e, g, source, client, cloud = setup
    cloud.upload(123, g)
    snapshot = cloud.manual_snapshot(123, g)
    real_pack = pack_current
    def pack(engine, game, archive):
        (source / "slot.sav").write_bytes(b"changed after confirmation")
        return real_pack(engine, game, archive)
    monkeypatch.setattr("umeyc_steamback.protocol.cloud.pack_current", pack)
    with pytest.raises(ValueError, match="确认后"):
        cloud.manual_transfer(123, g, "upload", snapshot)
    assert cloud.latest(123, g)["revision"] == snapshot["remote"]["revision"]
