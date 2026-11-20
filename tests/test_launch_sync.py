import pytest
import copy
import vdf
from umeyc_steamback.engine import Config, Engine
from umeyc_steamback.sync import WindowsCloud
from umeyc_steamback.protocol.custom import write_json
from tests.test_sync import setup


def test_disabled_launch_does_not_contact_quark(setup, monkeypatch):
    e, g, source, client, cloud = setup
    monkeypatch.setattr(client, "account", lambda: pytest.fail("disabled launch must be offline"))
    assert not cloud.prepare_game_launch(123, g)["synced"]
    activity = cloud.read_state(cloud.activity_path)[cloud.key(123, g)]
    assert activity["phase"] == "prelaunch"
    assert activity["message"] == "启动前检查：启动前云同步未开启"


def test_launch_download_new_cloud_after_previous_sync(setup):
    e, g, source, client, cloud = setup
    cloud.upload(123, g)
    previous = (source / "slot.sav").read_bytes()
    (source / "slot.sav").write_bytes(b"remote new chapter")
    cloud.upload(123, g)
    remote = cloud.latest(123, g)
    (source / "slot.sav").write_bytes(previous)
    # This machine last saw the earlier content; another device uploaded new.
    from umeyc_steamback.protocol.cloud import pack_current
    manifest = pack_current(e, g, e.data / "previous.zip")
    cloud.remember(123, g, manifest["content_hash"])
    cloud.set_auto_download(123, g, True)
    assert cloud.prepare_game_launch(123, g)["synced"]
    assert (source / "slot.sav").read_bytes() == b"remote new chapter"
    assert cloud.read_state()[cloud.key(123, g)]["synced_hash"] == remote["content_hash"]
    assert any(b["source"] == "before_cloud_sync" for b in e.backups(g))


def test_launch_preserves_local_progress_if_cloud_has_not_changed(setup):
    e, g, source, client, cloud = setup
    cloud.upload(123, g)
    cloud.set_auto_download(123, g, True)
    (source / "slot.sav").write_bytes(b"local new")
    result = cloud.prepare_game_launch(123, g)
    assert not result["synced"]
    assert (source / "slot.sav").read_bytes() == b"local new"


def test_first_launch_different_local_and_cloud_blocks_without_overwrite(setup):
    e, g, source, client, cloud = setup
    cloud.upload(123, g)
    cloud.state_path.unlink()
    cloud.set_auto_download(123, g, True)
    (source / "slot.sav").write_bytes(b"unsynced local")
    with pytest.raises(ValueError, match="手动选择"):
        cloud.prepare_game_launch(123, g)
    assert (source / "slot.sav").read_bytes() == b"unsynced local"
    assert not e.backups(g)


def test_launch_download_empty_folder_and_setting_is_account_scoped(setup, tmp_path):
    e, g, source, client, cloud = setup
    cloud.upload(123, g)
    cloud.set_auto_download(123, g, True)
    destination = tmp_path / "empty"
    destination.mkdir()
    e.configure(g, [str(destination)])
    assert cloud.launch_game(124, 42) is None
    assert cloud.launch_game(123, 43) is None
    assert cloud.prepare_game_launch(123, g)["synced"]
    assert (destination / "slot.sav").exists()
    assert not cloud.prepare_game_launch(123, g)["synced"]


def test_logout_or_expired_login_blocks_enabled_launch(setup):
    e, g, source, client, cloud = setup
    cloud.set_auto_download(123, g, True)
    cloud.uid = None
    with pytest.raises(ValueError, match="登录夸克"):
        cloud.prepare_game_launch(123, g)
    cloud.uid = client.user_id
    client.expired = True
    with pytest.raises(ValueError):
        cloud.prepare_game_launch(123, g)


def test_empty_cloud_allows_launch_and_off_setting_preserves_upload_flag(setup):
    e, g, source, client, cloud = setup
    cloud.set_auto(123, g, True)
    cloud.set_auto_download(123, g, True)
    result = cloud.prepare_game_launch(123, g)
    assert "暂无" in result["message"]
    cloud.set_auto_download(123, g, False)
    assert cloud.read_state()[cloud.key(123, g)]["auto_upload"]


def test_account_change_prevents_enabled_launch(setup):
    e, g, source, client, cloud = setup
    cloud.set_auto_download(123, g, True)
    cloud.active_account = 124
    with pytest.raises(ValueError, match="账号已变化"):
        cloud.prepare_game_launch(123, g)


def test_cached_quark_identity_cannot_silently_disable_launch_sync(setup):
    e, g, source, client, cloud = setup
    cloud.upload(123, g)
    cloud.set_auto_sync(123, g, True)
    cloud.uid = "cookie-alias"
    result = cloud.prepare_steam_launch(123, g)
    assert "本地与云端一致" in result["message"]
    assert cloud.uid == client.user_id


def test_changed_quark_account_does_not_use_another_accounts_configuration(setup):
    e, g, source, client, cloud = setup
    cloud.set_auto_sync(123, g, True)
    before = cloud.read_state()
    client.user_id = "another-account"
    with pytest.raises(ValueError, match="账号已变化"):
        cloud.prepare_steam_launch(123, g)
    assert cloud.read_state() == before
    assert (source / "slot.sav").read_bytes() == b"chapter one"


def test_stale_identity_network_failure_is_reported_instead_of_skipping_sync(setup, monkeypatch):
    from umeyc_steamback.protocol.quark import QuarkError
    e, g, source, client, cloud = setup
    cloud.set_auto_sync(123, g, True)
    cloud.uid = "cookie-alias"
    def offline():
        raise QuarkError("请检查网络")
    monkeypatch.setattr(client, "account", offline)
    with pytest.raises(QuarkError, match="检查网络"):
        cloud.prepare_steam_launch(123, g)
    record = cloud.read_state(cloud.activity_path)[cloud.key(123, g)]
    assert record["state"] == "error"
    assert "检查网络" in record["message"]
    assert (source / "slot.sav").read_bytes() == b"chapter one"


@pytest.mark.parametrize("cached_identity", [None, "cookie-alias"])
def test_independent_device_upload_is_pulled_by_restarted_launch_client(setup, tmp_path, cached_identity):
    e, g, source, client, cloud = setup
    cloud.upload(123, g)
    device = tmp_path / "second-device"
    destination = device / "saves"
    destination.mkdir(parents=True)
    other_engine = Engine(Config(str(device / "data"), str(device / "Steam")))
    other_engine.account_ids = {123}
    other_game = {**g, "install_root": str(device / "Steam")}
    manifests = device / "Steam" / "steamapps"
    manifests.mkdir(parents=True)
    (manifests / "appmanifest_42.acf").write_text(vdf.dumps({"AppState": {"installdir": "Test"}}))
    other_engine.configure(other_game, [str(destination)])
    # Independent client/service/storage; only the simulated remote is shared.
    other_client = copy.copy(client)
    other_client.upload = client.upload
    other_cloud = WindowsCloud(other_engine, other_client)
    other_cloud.download_current(123, other_game)
    other_cloud.set_auto_sync(123, other_game, True)
    baseline = other_cloud.read_state()[other_cloud.key(123, other_game)]["synced_hash"]

    (source / "slot.sav").write_bytes(b"chapter from another device")
    remote = cloud.upload(123, g)["remote"]
    assert remote["content_hash"] != baseline
    assert (destination / "slot.sav").read_bytes() == b"chapter one"
    restarted = WindowsCloud(other_engine, other_client)
    if cached_identity:
        restarted.uid = cached_identity
    assert restarted.prepare_steam_launch(123, other_game)["synced"]
    assert (destination / "slot.sav").read_bytes() == b"chapter from another device"
    assert restarted.read_state()[restarted.key(123, other_game)]["synced_hash"] == remote["content_hash"]
    assert any(b["source"] == "before_cloud_sync" for b in other_engine.backups(other_game))
    record = restarted.read_state(restarted.activity_path)[restarted.key(123, other_game)]
    assert record["synced"] and record["revision"] == remote["revision"]


def test_unified_toggle_enables_both_and_migrates_partial_legacy_settings_off(setup):
    e, g, source, client, cloud = setup
    cloud.set_auto_sync(123, g, True)
    saved = cloud.read_state()[cloud.key(123, g)]
    assert saved["auto_download"] and saved["auto_upload"] and saved["auto_sync"]
    cloud.set_auto_sync(123, g, False)
    assert not any(cloud.read_state()[cloud.key(123, g)][f] for f in ("auto_download", "auto_upload", "auto_sync"))
    write_json(cloud.state_path, {cloud.key(123, g): {"auto_upload": True, "game": g}})
    from umeyc_steamback.sync import WindowsCloud
    replacement = WindowsCloud(e, client)
    assert not replacement.read_state()[replacement.key(123, g)]["auto_sync"]
    assert not replacement.read_state()[replacement.key(123, g)]["auto_upload"]


def test_prelaunch_ignores_only_steam_wrapper_registry_and_restores_guard(setup, monkeypatch):
    e, g, source, client, cloud = setup
    calls = []
    def running(items, ignore_registry_for=None):
        calls.append(ignore_registry_for)
        return set() if ignore_registry_for == 42 else {42}
    monkeypatch.setattr("umeyc_steamback.steam.running_games", running)
    assert cloud.prepare_steam_launch(123, g)
    assert calls == [42]
    assert cloud.hook_game_id is None
    with pytest.raises(ValueError, match="正在运行"):
        e.guard(g)


def test_prelaunch_does_not_restore_a_stale_running_cache(setup):
    e, g, source, client, cloud = setup
    cloud.running = {(123, 42), (123, 43)}
    cloud.prepare_steam_launch(123, g)
    assert cloud.running == {(123, 43)}


def test_exit_upload_waits_for_stable_files_and_checks_account(setup):
    e, g, source, client, cloud = setup
    sleeps = []
    result = cloud.upload_after_exit(123, g, client.user_id, wait=sleeps.append)
    assert sleeps == [2]
    assert "上传" in result["message"]


def test_disabled_hook_runs_without_cloud_access_and_preserves_both_flags_off(setup, monkeypatch):
    e, g, source, client, cloud = setup
    cloud.set_auto_sync(123, g, True)
    cloud.set_auto_sync(123, g, False)
    monkeypatch.setattr(client, "account", lambda: pytest.fail("disabled hook must not access cloud"))
    assert cloud.prepare_steam_launch(123, g)["synced"] is False
    assert not cloud.read_state()[cloud.key(123, g)]["auto_upload"]
