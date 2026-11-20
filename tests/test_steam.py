import vdf
import os
import pytest
from contextlib import nullcontext
from types import SimpleNamespace
from umeyc_steamback import steam


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(vdf.dumps(data), encoding="utf-8")


def test_multiple_libraries_and_account_id32(tmp_path):
    root, external = tmp_path / "Steam", tmp_path / "中文游戏库"
    write(root / "config/loginusers.vdf", {"users": {str(steam.STEAM_ID_BASE + 123): {"PersonaName": "玩家", "MostRecent": "1"}}})
    write(root / "steamapps/libraryfolders.vdf", {"libraryfolders": {"1": {"path": str(external)}}})
    write(external / "steamapps/appmanifest_42.acf", {"AppState": {"appid": "42", "name": "测试游戏", "installdir": "Test"}})
    assert steam.accounts(root)[0]["id"] == 123
    assert steam.games(root)[0]["install_root"] == str(external)
    assert steam.games(root)[0]["game_name"] == "测试游戏"


def test_missing_steam_is_empty(tmp_path):
    assert steam.accounts(tmp_path) == []
    assert steam.games(tmp_path) == []


def test_system_process_names_do_not_resolve_inside_game_working_directory(tmp_path, monkeypatch):
    game = {"game_id": 42, "install_root": str(tmp_path), "install_dir": "Test"}
    folder = steam.game_directory(game) / "Game"
    folder.mkdir(parents=True)
    monkeypatch.chdir(folder)
    monkeypatch.setattr(steam.psutil, "process_iter", lambda *_: [
        SimpleNamespace(info={"exe": "Registry"}),
        SimpleNamespace(info={"exe": "MemCompression"}),
    ])
    assert steam.running_games([game], ignore_registry_for=42) == set()
    monkeypatch.setattr(steam.psutil, "process_iter", lambda *_: [
        SimpleNamespace(info={"exe": str(folder / "real-game.exe")}),
    ])
    assert steam.running_games([game], ignore_registry_for=42) == {42}


@pytest.mark.skipif(os.name != "nt", reason="Windows Steam registry")
def test_only_exited_wrapped_games_ignore_registry_and_real_processes_still_block(tmp_path, monkeypatch):
    import winreg
    games = [{"game_id": aid, "install_root": str(tmp_path), "install_dir": str(aid)} for aid in (42, 43)]
    monkeypatch.setattr(winreg, "OpenKey", lambda *args: nullcontext(None))
    monkeypatch.setattr(winreg, "QueryValueEx", lambda *args: (1, winreg.REG_DWORD))
    monkeypatch.setattr(steam.psutil, "process_iter", lambda *_: [])
    assert steam.running_games(games, ignore_registry_for={42}) == {43}
    assert steam.running_games(games) == {42, 43}
    monkeypatch.setattr(steam.psutil, "process_iter", lambda *_: [
        SimpleNamespace(info={"exe": str(steam.game_directory(games[0]) / "game.exe")}),
    ])
    assert steam.running_games(games, ignore_registry_for={42}) == {42, 43}
