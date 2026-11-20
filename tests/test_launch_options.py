from pathlib import Path
from unittest.mock import patch

import pytest
import vdf

from umeyc_steamback.engine import Config, Engine, read_json
from umeyc_steamback.launch_options import LaunchOptions


@pytest.fixture
def manager(tmp_path, monkeypatch):
    engine = Engine(Config(str(tmp_path / "data"), str(tmp_path / "Steam")))
    manager = LaunchOptions(engine)
    path = manager.path(123)
    path.parent.mkdir(parents=True)
    tree = {"UserLocalConfigStore": {"Software": {"Valve": {"Steam": {"apps": {
        "42": {"LaunchOptions": '-dx12 --profile "中文 玩家"', "LastPlayed": "9"},
        "43": {"LaunchOptions": "do not touch"}}}}}}, "other": "preserve"}
    path.write_text(vdf.dumps(tree), encoding="utf-8")
    monkeypatch.setattr("umeyc_steamback.launch_options.steam_is_running", lambda: False)
    return manager, path


def test_install_is_idempotent_and_restores_exact_original_options(manager):
    manager, path = manager
    old, generated = manager.preview(123, 42)
    assert generated.endswith('%command% -dx12 --profile "中文 玩家"')
    manager.update(123, 42)
    assert manager.installed(123, 42)
    assert manager.read(123, 42)[5] == generated
    assert manager.read(123, 43)[5] == "do not touch"
    before = path.read_bytes()
    manager.update(123, 42)
    assert path.read_bytes() == before
    manager.update(123, 42, restore=True)
    assert manager.read(123, 42)[5] == old
    assert vdf.loads(path.read_text())["other"] == "preserve"
    assert list((manager.engine.data / "steam_config_backups").glob("*.vdf"))


def test_live_copy_records_original_but_does_not_modify_steam(manager):
    manager, path = manager
    before = path.read_bytes()
    old, generated = manager.preview(123, 42)
    manager.remember_preview(123, 42, generated)
    assert path.read_bytes() == before
    assert manager.configured(123, 42)["original"] == old
    assert manager.preview(123, 42)[1] == generated
    manager.update(123, 42)
    manager.update(123, 42, restore=True)
    assert manager.read(123, 42)[5] == old


def test_running_steam_rejects_direct_write_without_closing_it(manager, monkeypatch):
    manager, path = manager
    before = path.read_bytes()
    monkeypatch.setattr("umeyc_steamback.launch_options.steam_is_running", lambda: True)
    assert manager.preview(123, 42)[1]
    with pytest.raises(ValueError, match="完全退出"):
        manager.update(123, 42)
    assert path.read_bytes() == before


def test_preserve_existing_wrapper_and_refuse_external_changes(manager):
    manager, path = manager
    path.write_text(path.read_text().replace('-dx12 --profile \\"中文 玩家\\"', 'existing %command% -arg'), encoding="utf-8")
    # Use the metadata reader to ensure edits are serialized with VDF escaping.
    _, _, tree, node, field, *_ = manager.read(123, 42)
    del node[field]
    node[field] = '"C:/Mod Loader/mod.exe" %command% -flag'
    path.write_text(vdf.dumps(tree), encoding="utf-8")
    old, generated = manager.preview(123, 42)
    assert generated.endswith(old)
    manager.update(123, 42)
    path.write_text(path.read_text().replace('-flag', '-edited'), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="停止恢复"):
        manager.update(123, 42, restore=True)
    assert path.read_bytes() == before


def test_absent_launch_options_remains_absent_after_restore(manager):
    manager, path = manager
    manager.update(123, 44)
    manager.update(123, 44, restore=True)
    assert manager.read(123, 44)[-1] is False


def test_configuration_race_cancels_without_overwriting(manager, monkeypatch):
    manager, path = manager
    calls = []
    def running():
        calls.append(1)
        return len(calls) > 1
    monkeypatch.setattr("umeyc_steamback.launch_options.steam_is_running", running)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="取消修改"):
        manager.update(123, 42)
    assert path.read_bytes() == before
    manager.recover()
    assert not manager.configured(123, 42)
