import vdf
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
