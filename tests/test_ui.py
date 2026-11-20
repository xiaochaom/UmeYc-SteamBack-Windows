import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import time
import pytest
from PySide6.QtWidgets import QApplication, QDialog, QPushButton
from umeyc_steamback.app import Window, PathsDialog, MappingDialog
from umeyc_steamback.protocol.custom import write_json

# Qt requires one QApplication for the entire process. Retain a strong
# reference so individual test scopes cannot destroy it between windows.
_APPLICATION = QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def cleanup_qt_windows():
    yield
    from PySide6.QtCore import QCoreApplication, QEvent, QThreadPool
    # Keep Qt workers, queued signals and widget destruction inside the test
    # event loop instead of leaving QObjects to Python interpreter teardown.
    assert QThreadPool.globalInstance().waitForDone(5000)
    _APPLICATION.processEvents()
    for widget in _APPLICATION.topLevelWidgets():
        if isinstance(widget, Window):
            widget.monitor.stop()
            widget.command_timer.stop()
            widget.quitting = True
        widget.close()
        widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    _APPLICATION.processEvents()


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


def test_game_groups_and_launch_uses_normal_steam_entry(tmp_path, monkeypatch):
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
    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url.toString()) or True)
    monkeypatch.setattr(w.cloud, "prepare_game_launch", lambda *_: (_ for _ in ()).throw(AssertionError("Steam wrapper owns preflight")))
    w.launch_game()
    app.processEvents()
    assert opened == ["steam://rungameid/43"]
    drain(app, w)
    w.close()


def test_hidden_window_skips_page_render_and_session_host_exits_only_when_idle(tmp_path, monkeypatch):
    app = _APPLICATION
    monkeypatch.setattr("umeyc_steamback.app.Window.refresh", lambda _: None)
    w = Window(tmp_path / "data")
    w.monitor.stop()
    w.hide_window()
    monkeypatch.setattr(w.engine, "backups", lambda _: (_ for _ in ()).throw(AssertionError("hidden page should not refresh")))
    w.render_game()
    assert w.page_dirty and not w.centralWidget().updatesEnabled()
    assert w.monitor.interval() == 5000
    w.session_host = True
    w.last_session_work = time.monotonic() - 60
    quit_called = []
    monkeypatch.setattr(w, "quit_app", lambda: quit_called.append(1))
    w.sessions[(123, 42)] = 1
    w.maybe_exit_session()
    assert not quit_called
    w.sessions.clear()
    w.cloud.running = {(123, 42)}
    w.maybe_exit_session()
    assert not quit_called
    w.cloud.running.clear()
    w.upload_queue.pending["test"] = {}
    w.maybe_exit_session()
    assert not quit_called
    w.upload_queue.pending.clear()
    w.maybe_exit_session()
    assert quit_called == [1]
    w.close()


def test_ipc_sync_is_acknowledged_only_after_background_result(tmp_path):
    import subprocess
    import sys
    from umeyc_steamback.ipc import CommandServer, server_name
    app = _APPLICATION
    server = CommandServer(server_name(tmp_path))
    received = []
    server.command.connect(lambda command, reply: received.append((command, reply)))
    # The production client is a separate process. A Python thread using Qt's
    # blocking socket wait can hold the GIL and starve the server event loop.
    code = "from PySide6.QtCore import QCoreApplication; from umeyc_steamback.ipc import request; import sys,json; app=QCoreApplication([]); print(json.dumps(request(sys.argv[1], {'action':'prepare','account':123,'game_id':42}, 5000)))"
    client = subprocess.Popen([sys.executable, "-c", code, server_name(tmp_path)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        until = time.monotonic() + 4
        while not received and time.monotonic() < until:
            app.processEvents()
            time.sleep(.01)
        assert received and client.poll() is None
        received[0][1]({"ok": True})
        while client.poll() is None and time.monotonic() < until:
            app.processEvents()
            time.sleep(.01)
        output, error = client.communicate(timeout=2)
        assert client.returncode == 0, error
        assert output.strip() == '{"ok": true}'
    finally:
        if client.poll() is None:
            client.kill()
            client.wait()
    server.server.close()


def test_game_page_has_additive_launch_configuration_without_restore_action(tmp_path, monkeypatch):
    monkeypatch.setattr("umeyc_steamback.app.Window.refresh", lambda _: None)
    w = Window(tmp_path / "data")
    w.monitor.stop()
    texts = [b.text() for b in w.findChildren(QPushButton)]
    assert "配置 Steam 启动项" in texts
    assert not any("恢复原启动项" in t or "复制原启动项" in t for t in texts)
    w.close()
