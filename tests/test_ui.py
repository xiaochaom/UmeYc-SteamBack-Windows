import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import time
from PySide6.QtWidgets import QApplication, QDialog, QPushButton
from umeyc_steamback.app import Window, PathsDialog, MappingDialog
from umeyc_steamback.protocol.custom import write_json

# Qt requires one QApplication for the entire process. Retain a strong
# reference so individual test scopes cannot destroy it between windows.
_APPLICATION = QApplication.instance() or QApplication([])


def drain(app, window, timeout=5):
    deadline = time.monotonic() + timeout
    for _ in range(3):
        app.processEvents()
        time.sleep(.01)
    while window.busy and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.01)
    assert not window.busy


def test_window_worker_and_record_save_only_closes_record_dialog(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    source = tmp_path / "saves"
    source.mkdir()
    (source / "slot.sav").write_bytes(b"test")
    write_json(tmp_path / "data/ui.json", {"steam": str(tmp_path / "Steam"), "account": 123})
    monkeypatch.setattr("umeyc_steamback.steam.running_games", lambda _: set())
    monkeypatch.setattr("umeyc_steamback.app.Window.refresh", lambda _: None)
    window = Window(tmp_path / "data")
    window.monitor.stop()
    game = {"game_id": 42, "game_name": "测试", "install_root": str(tmp_path / "Steam"), "install_dir": "Test"}
    window.engine.configure(game, [str(source)])
    record = window.engine.backup(game)
    window.items = [game]
    window.filter_games()
    window.show()
    drain(app, window)
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QLineEdit
    def rename():
        dialog = QApplication.activeModalWidget()
        assert isinstance(dialog, QDialog)
        dialog.findChild(QLineEdit).setText("改名后")
        next(b for b in dialog.findChildren(QPushButton) if b.text() == "保存名称").click()
    QTimer.singleShot(0, rename)
    window.open_record(0)
    assert window.isVisible()
    assert window.game["game_id"] == 42
    assert window.engine.backups(game)[0]["name"] == "改名后"
    drain(app, window)
    window.close()


def test_path_and_mapping_dialog_preserves_order(tmp_path):
    app = QApplication.instance() or QApplication([])
    dialog = PathsDialog(["C:/Saves/A", "C:/Saves/B"], None)
    dialog.items.setCurrentRow(1)
    dialog.move(-1)
    assert dialog.paths() == ["C:/Saves/B", "C:/Saves/A"]
    mapping = MappingDialog(["custom-0", "custom-1"], dialog.paths(), None)
    assert mapping.paths() == dialog.paths()


def test_game_groups_and_launch_waits_for_cloud_sync(tmp_path, monkeypatch):
    import threading
    from PySide6.QtGui import QDesktopServices
    app = QApplication.instance() or QApplication([])
    write_json(tmp_path / "data/ui.json", {"steam": str(tmp_path / "Steam"), "account": 123})
    monkeypatch.setattr("umeyc_steamback.app.Window.refresh", lambda _: None)
    monkeypatch.setattr("umeyc_steamback.steam.running_games", lambda _: set())
    w = Window(tmp_path / "data")
    w.monitor.stop()
    g = {"game_id": 42, "game_name": "有云游戏", "install_root": str(tmp_path / "Steam"), "steam_cloud": True}
    w.items = [g, {**g, "game_id": 43, "game_name": "无云游戏", "steam_cloud": False}]
    w.filter_games()
    assert w.games_list.item(0).text().startswith("不支持 Steam 云存档")
    assert w.games_list.item(1).data(256)["game_id"] == 43
    assert w.games_list.item(2).text().startswith("支持 Steam 云存档")
    drain(app, w)
    opened, event = [], threading.Event()
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url.toString()) or True)
    def prepare(account, game):
        assert event.wait(3)
        return {"message": "已同步", "synced": True}
    monkeypatch.setattr(w.cloud, "prepare_game_launch", prepare)
    w.launch_game()
    app.processEvents()
    assert not opened
    event.set()
    drain(app, w)
    assert opened == ["steam://rungameid/43"]
    w.close()
