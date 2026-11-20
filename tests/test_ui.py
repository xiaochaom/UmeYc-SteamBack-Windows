import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import time
import pytest
from PySide6.QtWidgets import QApplication, QDialog, QPushButton
from umeyc_steamback.app import Window, PathsDialog
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
            assert widget.update_pool.waitForDone(5000)
            widget.update_retry.stop()
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


def test_auto_sync_idle_polling_never_disables_the_window(tmp_path, monkeypatch):
    from PySide6.QtCore import QObject, QEvent
    monkeypatch.setattr(Window, "refresh", lambda _: None)
    w = Window(tmp_path / "data")
    w.monitor.stop()
    w.engine.account_ids = {123}
    w.cloud.uid = "uid"
    w.game = {"game_id": 42, "game_name": "测试"}
    w.items = [w.game]
    monkeypatch.setattr(w.cloud, "read_state", lambda: {"uid:123:42": {"auto_upload": True}})
    monkeypatch.setattr("umeyc_steamback.app.desktop.check_steam_account", lambda _: None)
    monkeypatch.setattr("umeyc_steamback.app.steam.running_games", lambda _: set())
    monkeypatch.setattr(w.upload_queue, "observe", lambda *args, **kwargs: None)
    monkeypatch.setattr(w.upload_queue, "next", lambda *args: None)
    changes = []
    class Watcher(QObject):
        def eventFilter(self, obj, event):
            if event.type() == QEvent.EnabledChange:
                changes.append(obj.isEnabled())
            return False
    watcher = Watcher()
    w.header.installEventFilter(watcher)
    w.splitter.installEventFilter(watcher)
    w.status.setText("自动云同步已开启")
    for _ in range(3):
        w.monitor_games()
        assert w.header.isEnabled() and w.splitter.isEnabled()
        drain(_APPLICATION, w)
    assert changes == []
    assert w.status.text() == "自动云同步已开启"
    # A real save write still prevents concurrent edits.
    w.task("正在上传…", lambda: None)
    assert not w.header.isEnabled() and not w.splitter.isEnabled()
    drain(_APPLICATION, w)
    assert w.header.isEnabled() and w.splitter.isEnabled()


def test_rendering_auto_sync_checkbox_does_not_write_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(Window, "refresh", lambda _: None)
    w = Window(tmp_path / "data")
    w.monitor.stop()
    w.engine.account_ids = {123}
    w.cloud.uid = "uid"
    w.game = {"game_id": 42, "game_name": "测试"}
    monkeypatch.setattr(w.cloud, "read_state", lambda: {"uid:123:42": {"auto_sync": True}})
    monkeypatch.setattr(w, "task", lambda *args, **kwargs: None)
    signals = []
    w.auto_sync.toggled.connect(signals.append)
    w.render_game()
    assert w.auto_sync.isChecked()
    assert signals == []


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


def test_path_dialog_preserves_order(tmp_path):
    app = QApplication.instance() or QApplication([])
    dialog = PathsDialog(["C:/Saves/A", "C:/Saves/B"], None)
    dialog.items.setCurrentRow(1)
    dialog.move(-1)
    assert dialog.paths() == ["C:/Saves/B", "C:/Saves/A"]


def test_unified_game_list_filters_compatibility_tools_and_launch_uses_normal_steam_entry(tmp_path, monkeypatch):
    from PySide6.QtGui import QDesktopServices
    app = QApplication.instance() or QApplication([])
    write_json(tmp_path / "data/ui.json", {"steam": str(tmp_path / "Steam"), "account": 123})
    monkeypatch.setattr("umeyc_steamback.app.Window.refresh", lambda _: None)
    monkeypatch.setattr("umeyc_steamback.steam.running_games", lambda _: set())
    w = Window(tmp_path / "data")
    w.monitor.stop()
    g = {"game_id": 42, "game_name": "DARK SOULS III", "install_root": str(tmp_path / "Steam"), "steam_cloud": True}
    w.items = [g, {**g, "game_id": 43, "game_name": "Shadows", "steam_cloud": False},
               {**g, "game_id": 44, "game_name": "Proton - Experimental"},
               {**g, "game_id": 45, "game_name": "Portal 2", "steam_cloud": None}]
    w.filter_games()
    assert [w.games_list.item(i).data(256)["game_name"] for i in range(w.games_list.count())] == [
        "DARK SOULS III", "Portal 2", "Shadows"]
    w.games_list.setCurrentRow(2)
    w.search.setText("43")
    assert w.games_list.count() == 1 and w.game["game_id"] == 43
    w.search.clear()
    assert w.games_list.count() == 3 and w.game["game_id"] == 43
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


@pytest.mark.parametrize('choice,expected', [('关闭', []), ('使用本地存档上传', ['upload']), ('使用云端存档下载', ['download'])])
def test_manual_sync_requires_explicit_direction_and_close_never_writes(tmp_path, monkeypatch, choice, expected):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QLabel
    monkeypatch.setattr('umeyc_steamback.app.Window.refresh', lambda _: None)
    w = Window(tmp_path / 'data')
    w.monitor.stop()
    events = []
    monkeypatch.setattr(w, 'upload', lambda: events.append('upload'))
    monkeypatch.setattr(w, 'download', lambda: events.append('download'))
    def select():
        dialog = _APPLICATION.activeModalWidget()
        assert isinstance(dialog, QDialog)
        assert any('本地存档时间：' in x.text() and '云端存档时间：' in x.text() for x in dialog.findChildren(QLabel))
        assert not events
        next(b for b in dialog.findChildren(QPushButton) if b.text() == choice).click()
    QTimer.singleShot(0, select)
    w.manual_sync_dialog({'game_id': 42}, 123, {'save_time': 1770000000000})
    assert events == expected
    w.close()


def test_update_check_does_not_occupy_sync_worker_or_popup_when_hidden(tmp_path, monkeypatch):
    import threading
    from PySide6.QtGui import QDesktopServices
    from umeyc_steamback.updates import UpdateResult
    monkeypatch.setattr('umeyc_steamback.app.Window.refresh', lambda _: None)
    entered, finish = threading.Event(), threading.Event()
    calls = []
    def check():
        calls.append(1)
        entered.set()
        assert finish.wait(4)
        return UpdateResult('0.2.0', True, 'https://github.com/xiaochaom/UmeYc-SteamBack-Windows/releases/tag/v0.2.0')
    monkeypatch.setattr('umeyc_steamback.app.check_updates', check)
    w = Window(tmp_path / 'data')
    w.monitor.stop()
    w.hide_window()
    try:
        w.check_for_updates()
        assert entered.wait(1)
        assert w.update_busy and not w.busy
        w.check_for_updates()  # A second click must not start a duplicate request.
        synced = []
        w.task('', lambda: 'synced', synced.append, quiet=True)
        drain(_APPLICATION, w)
        assert synced == ['synced'] and calls == [1]
    finally:
        finish.set()
        assert w.update_pool.waitForDone(2000)
        _APPLICATION.processEvents()
    assert not w.update_busy and not w.isVisible()
    assert _APPLICATION.activeModalWidget() is None
    assert '0.2.0' in w.update_status.text()
    assert w.download_update_button.text() == '下载并更新'
    w.close()


def test_update_failure_preserves_sync_status_and_previous_download(tmp_path, monkeypatch):
    from umeyc_steamback.updates import UpdateResult
    monkeypatch.setattr('umeyc_steamback.app.Window.refresh', lambda _: None)
    monkeypatch.setattr('umeyc_steamback.app.check_updates', lambda: UpdateResult('0.2.0', True, 'https://github.com/xiaochaom/UmeYc-SteamBack-Windows/releases/tag/v0.2.0'))
    w = Window(tmp_path / 'data')
    w.monitor.stop()
    w.check_for_updates()
    assert w.update_pool.waitForDone(2000)
    _APPLICATION.processEvents()
    w.status.setText('存档同步中')
    def offline():
        raise ValueError('无法检查更新，请检查网络后重试')
    monkeypatch.setattr('umeyc_steamback.app.check_updates', offline)
    w.check_for_updates()
    assert w.update_pool.waitForDone(2000)
    _APPLICATION.processEvents()
    assert '检查网络' in w.update_status.text()
    assert w.status.text() == '存档同步中'
    assert w.update_url and not w.download_update_button.isHidden()
    assert w.check_update_button.isEnabled()
    assert _APPLICATION.activeModalWidget() is None
    w.close()


def test_reopening_existing_background_host_checks_updates_again(tmp_path, monkeypatch):
    monkeypatch.setattr('umeyc_steamback.app.Window.refresh', lambda _: None)
    w = Window(tmp_path / 'data')
    w.monitor.stop()
    events = []
    monkeypatch.setattr(w, 'show_window', lambda: events.append('show'))
    monkeypatch.setattr(w, 'check_for_updates', lambda: events.append('check'))
    w.handle_command({'action': 'show'}, lambda reply: events.append(reply))
    assert events == ['show', 'check', {'ok': True}]
    w.close()


def test_account_labels_show_nicknames_but_keep_separate_account_ids(tmp_path, monkeypatch):
    monkeypatch.setattr(Window, "refresh", lambda _: None)
    monkeypatch.setattr("umeyc_steamback.steam.accounts", lambda _: [
        {"id": 123, "name": "玩家昵称"}, {"id": 456, "name": "另一个玩家"}])
    w = Window(tmp_path / "data")
    w.monitor.stop()
    w.load_accounts(456)
    assert [w.account_box.itemText(i) for i in range(w.account_box.count())] == ["玩家昵称", "另一个玩家"]
    assert w.account_box.currentData() == 456 and w.engine.account == 456
    w.auth = {"logged_in": True, "nickname": "夸克昵称", "user_id": "private-id"}
    w.update_auth()
    assert w.login_button.text() == "夸克昵称"
    w.auth["nickname"] = None
    w.update_auth()
    assert w.login_button.text() == "夸克用户"
    w.load_accounts(789)
    assert w.account_box.currentText() == "Steam 用户"
    assert w.engine.account == 789


def test_cloud_download_uses_configured_paths_without_dialog(tmp_path, monkeypatch):
    monkeypatch.setattr(Window, "refresh", lambda _: None)
    w = Window(tmp_path / "data", backup_root=tmp_path / "backups")
    w.monitor.stop()
    w.engine.account_ids = {123}
    w.game = {"game_id": 42, "game_name": "测试"}
    source = tmp_path / "saves"
    source.mkdir()
    w.engine.configure(w.game, [str(source)])
    monkeypatch.setattr(QDialog, "exec", lambda _: pytest.fail("已配置路径不应弹窗"))
    calls = []
    monkeypatch.setattr(w, "start_manual_transfer", lambda account, game, direction: calls.append((account, game)))
    w.download()
    assert calls == [(123, w.game)]
    assert w.engine.settings(w.game)["paths"] == [str(source)]


@pytest.mark.parametrize("accepted", [True, False])
def test_cloud_download_asks_for_actual_path_once_only_if_unconfigured(tmp_path, monkeypatch, accepted):
    monkeypatch.setattr(Window, "refresh", lambda _: None)
    w = Window(tmp_path / "data")
    w.monitor.stop()
    w.engine.account_ids = {123}
    w.game = {"game_id": 42, "game_name": "测试"}
    source = tmp_path / "saves"
    source.mkdir()
    dialogs, calls = [], []
    def select(dialog):
        dialogs.append(dialog.windowTitle())
        dialog.items.addItem(str(source))
        return QDialog.Accepted if accepted else QDialog.Rejected
    monkeypatch.setattr(PathsDialog, "exec", select)
    monkeypatch.setattr(w, "start_manual_transfer", lambda *args: calls.append(args))
    w.download()
    assert dialogs == ["选择游戏实际存档路径"]
    assert bool(calls) == accepted
    assert bool(w.engine.settings(w.game).get("paths")) == accepted


def test_executable_icon_has_small_and_large_cloud_save_images():
    from pathlib import Path
    from PySide6.QtGui import QIcon
    import umeyc_steamback.app as module
    icon = QIcon(str(Path(module.__file__).parent / "assets/cloud-save.ico"))
    assert not icon.isNull()
    assert {16, 32, 48, 256} <= {size.width() for size in icon.availableSizes()}
    assert not icon.pixmap(256, 256).isNull()


@pytest.mark.parametrize("background", [False, True])
def test_completed_sync_keeps_cloud_save_and_upload_times(tmp_path, monkeypatch, background):
    from umeyc_steamback.app import date
    monkeypatch.setattr(Window, "refresh", lambda _: None)
    w = Window(tmp_path / "data")
    w.monitor.stop()
    w.engine.account_ids = {123}
    w.game = {"game_id": 42, "game_name": "测试"}
    w.items = [w.game]
    w.cloud.uid = "uid"
    monkeypatch.setattr(w, "task", lambda *args, **kwargs: None)
    w.remote = {"save_time": 1000, "uploaded_at": 2000}
    remote = {"save_time": 1700000000000, "uploaded_at": 1700000060000}
    w.background_mode = background
    w.synced({"message": "同步成功", "remote": remote, "uid": "uid", "account": 123, "game_id": 42})
    assert w.remote == remote
    w.background_mode = False
    w.render_game()
    # A refresh/reselection of the same game must not clear this result.
    w.filter_games()
    assert f"云端存档时间：{date(remote['save_time'])}" in w.times.text()
    assert f"云端上传时间：{date(remote['uploaded_at'])}" in w.times.text()
    assert "暂无" not in "\n".join(w.times.text().splitlines()[1:])


@pytest.mark.parametrize("field,value", [("game_id", 43), ("account", 456), ("uid", "another-user")])
def test_sync_result_for_another_game_or_account_does_not_replace_display(tmp_path, monkeypatch, field, value):
    monkeypatch.setattr(Window, "refresh", lambda _: None)
    w = Window(tmp_path / "data")
    w.monitor.stop()
    w.engine.account_ids = {123}
    w.game = {"game_id": 42, "game_name": "测试"}
    w.cloud.uid = "uid"
    w.remote = {"save_time": 1234, "uploaded_at": 2345}
    result = {"message": "同步成功", "remote": {"save_time": 9999, "uploaded_at": 9999},
              "uid": "uid", "account": 123, "game_id": 42, field: value}
    w.synced(result)
    assert w.remote == {"save_time": 1234, "uploaded_at": 2345}


def finish_update_jobs(window):
    for _ in range(4):
        assert window.update_pool.waitForDone(5000)
        _APPLICATION.processEvents()


def test_in_app_update_downloads_then_hands_off_and_exits_only_when_ready(tmp_path, monkeypatch):
    from umeyc_steamback.updates import UpdateResult
    from umeyc_steamback.updater import PreparedUpdate
    import threading
    monkeypatch.setattr(Window, "refresh", lambda _: None)
    monkeypatch.setattr("umeyc_steamback.app.can_self_update", lambda: True)
    monkeypatch.setattr("umeyc_steamback.app.steam.running_games", lambda _: set())
    w = Window(tmp_path / "data")
    w.monitor.stop()
    w.update_info = UpdateResult("0.2.0", True, "https://github.com/example")
    prepared = PreparedUpdate("0.2.0", tmp_path / "data/updates/work", tmp_path / "payload")
    calls, entered, release = [], threading.Event(), threading.Event()
    def download(result, data, progress):
        assert result is w.update_info and data == w.data
        progress(1, 2)
        calls.append("download")
        return prepared
    def handoff(package, target):
        assert package == prepared
        calls.append("handoff")
        entered.set()
        assert release.wait(4)
    monkeypatch.setattr("umeyc_steamback.app.download_update", download)
    monkeypatch.setattr("umeyc_steamback.app.launch_update", handoff)
    monkeypatch.setattr(QApplication, "quit", lambda *args: calls.append("quit"))
    try:
        w.open_update()
        assert w.update_pool.waitForDone(1000)
        _APPLICATION.processEvents()
        assert entered.wait(1)
        assert calls == ["download", "handoff"]
        assert w.installing_update and not w.monitor.isActive()
        replies = []
        w.handle_command({"action": "prepare", "account": 123, "game_id": 42}, replies.append)
        assert not replies[0]["ok"] and "更新" in replies[0]["error"]
        w.task("", lambda: pytest.fail("update must block new save work"))
    finally:
        release.set()
        finish_update_jobs(w)
    assert calls == ["download", "handoff", "quit"]


@pytest.mark.parametrize("active", ["save_task", "steam_game", "launch_queue"])
def test_ready_update_waits_for_games_and_sync_without_quitting(tmp_path, monkeypatch, active):
    from umeyc_steamback.updater import PreparedUpdate
    monkeypatch.setattr(Window, "refresh", lambda _: None)
    monkeypatch.setattr("umeyc_steamback.app.steam.running_games", lambda _: {42} if active == "steam_game" else set())
    monkeypatch.setattr("umeyc_steamback.app.launch_update", lambda *_: pytest.fail("must wait"))
    w = Window(tmp_path / "data")
    w.monitor.stop()
    w.prepared_update = PreparedUpdate("0.2.0", tmp_path / "work", tmp_path / "payload")
    if active == "save_task": w.busy = True
    if active == "launch_queue": w.launch_requests.append(({}, lambda _: None))
    w.install_prepared_update()
    assert not w.installing_update and not w.quitting
    assert ("游戏仍在运行" if active == "steam_game" else "等待当前存档操作") in w.update_status.text()
    assert w.update_retry.isActive()
    w.busy = False


@pytest.mark.parametrize("cached", ["session", "running", "both"])
def test_stale_game_cache_cannot_block_restart_update(tmp_path, monkeypatch, cached):
    from umeyc_steamback.updater import PreparedUpdate
    monkeypatch.setattr(Window, "refresh", lambda _: None)
    monkeypatch.setattr("umeyc_steamback.app.steam.running_games", lambda _: set())
    monkeypatch.setattr("umeyc_steamback.app.check_other_processes", lambda _: None)
    w = Window(tmp_path / "data")
    w.monitor.stop()
    w.prepared_update = PreparedUpdate("0.2.0", tmp_path / "work", tmp_path / "payload")
    if cached in ("session", "both"): w.sessions[(123, 42)] = 1
    if cached in ("running", "both"): w.cloud.running.add((123, 42))
    calls = []
    monkeypatch.setattr("umeyc_steamback.app.launch_update", lambda *_: calls.append("handoff"))
    monkeypatch.setattr(QApplication, "quit", lambda *args: calls.append("quit"))
    w.install_prepared_update()
    finish_update_jobs(w)
    assert calls == ["handoff", "quit"]
    assert not w.sessions and not w.cloud.running


def test_restart_update_resumes_without_a_second_click_after_save_work_finishes(tmp_path, monkeypatch):
    from umeyc_steamback.updater import PreparedUpdate
    monkeypatch.setattr(Window, "refresh", lambda _: None)
    monkeypatch.setattr("umeyc_steamback.app.steam.running_games", lambda _: set())
    monkeypatch.setattr("umeyc_steamback.app.check_other_processes", lambda _: None)
    w = Window(tmp_path / "data")
    w.monitor.stop()
    w.prepared_update = PreparedUpdate("0.2.0", tmp_path / "work", tmp_path / "payload")
    calls = []
    monkeypatch.setattr("umeyc_steamback.app.launch_update", lambda *_: calls.append("handoff"))
    monkeypatch.setattr(QApplication, "quit", lambda *args: calls.append("quit"))
    w.busy = True
    w.install_prepared_update()
    assert w.update_retry.isActive() and not calls
    w.busy = False
    # Deliver the same timer callback without relying on wall-clock timing.
    w.update_retry.timeout.emit()
    finish_update_jobs(w)
    assert calls == ["handoff", "quit"]
    assert not w.update_retry.isActive()


def test_update_detects_configured_game_even_when_not_in_visible_list(tmp_path, monkeypatch):
    from umeyc_steamback.updater import PreparedUpdate
    monkeypatch.setattr(Window, "refresh", lambda _: None)
    w = Window(tmp_path / "data")
    w.monitor.stop()
    w.engine.account_ids = {123}
    game = {"game_id": 42, "game_name": "正在运行的游戏"}
    monkeypatch.setattr(w.engine, "configured_games", lambda: [game])
    monkeypatch.setattr("umeyc_steamback.app.steam.running_games", lambda games: {g["game_id"] for g in games})
    monkeypatch.setattr("umeyc_steamback.app.launch_update", lambda *_: pytest.fail("game still running"))
    w.prepared_update = PreparedUpdate("0.2.0", tmp_path / "work", tmp_path / "payload")
    w.install_prepared_update()
    assert not w.installing_update and w.update_retry.isActive()
    assert "正在运行的游戏" in w.update_status.text()


def test_update_persists_just_exited_game_upload_intent_before_clearing_cache(tmp_path, monkeypatch):
    from umeyc_steamback.updater import PreparedUpdate
    from umeyc_steamback.protocol.retry_queue import UploadQueue
    monkeypatch.setattr(Window, "refresh", lambda _: None)
    monkeypatch.setattr("umeyc_steamback.app.steam.running_games", lambda _: set())
    monkeypatch.setattr("umeyc_steamback.app.check_other_processes", lambda _: None)
    w = Window(tmp_path / "data")
    w.monitor.stop()
    w.engine.account_ids = {123}
    w.cloud.uid = "uid"
    w.items = [{"game_id": 42, "game_name": "测试游戏"}]
    monkeypatch.setattr(w.cloud, "read_state", lambda: {"uid:123:42": {"auto_upload": True, "synced_hash": "original-baseline"}})
    w.upload_queue.previous = (("uid", 123), {42})
    w.cloud.running = {(123, 42)}
    w.prepared_update = PreparedUpdate("0.2.0", tmp_path / "work", tmp_path / "payload")
    def launch(*_):
        restored = UploadQueue(w.data)
        assert restored.pending["uid:123:42"]["base_hash"] == "original-baseline"
    monkeypatch.setattr("umeyc_steamback.app.launch_update", launch)
    monkeypatch.setattr(QApplication, "quit", lambda *args: None)
    w.install_prepared_update()
    finish_update_jobs(w)
    assert w.quitting


def test_update_handoff_failure_keeps_old_ui_usable(tmp_path, monkeypatch):
    from umeyc_steamback.updater import PreparedUpdate
    monkeypatch.setattr(Window, "refresh", lambda _: None)
    monkeypatch.setattr("umeyc_steamback.app.steam.running_games", lambda _: set())
    def fail(*_):
        raise ValueError("更新进程未能启动")
    monkeypatch.setattr("umeyc_steamback.app.launch_update", fail)
    w = Window(tmp_path / "data")
    w.monitor.stop()
    w.prepared_update = PreparedUpdate("0.2.0", tmp_path / "work", tmp_path / "payload")
    w.install_prepared_update()
    finish_update_jobs(w)
    assert not w.installing_update and not w.quitting
    assert w.header.isEnabled() and w.splitter.isEnabled()
    assert "未能启动" in w.update_status.text()


def test_update_download_error_does_not_affect_save_worker(tmp_path, monkeypatch):
    from umeyc_steamback.updates import UpdateResult
    monkeypatch.setattr(Window, "refresh", lambda _: None)
    monkeypatch.setattr("umeyc_steamback.app.can_self_update", lambda: True)
    def fail(*_):
        raise ValueError("更新包校验失败")
    monkeypatch.setattr("umeyc_steamback.app.download_update", fail)
    w = Window(tmp_path / "data")
    w.monitor.stop()
    w.update_info = UpdateResult("0.2.0", True, "https://github.com/example")
    w.status.setText("存档同步中")
    w.open_update()
    finish_update_jobs(w)
    assert not w.update_downloading and not w.installing_update
    assert w.check_update_button.isEnabled() and w.download_update_button.isEnabled()
    assert w.status.text() == "存档同步中" and "校验失败" in w.update_status.text()
    assert not w.prepared_update


@pytest.mark.parametrize("direction,local,remote_time,confirm,transferred", [
    ("upload", 1000, 2000, False, False), ("upload", 1000, 2000, True, True),
    ("download", 2000, 1000, False, False), ("download", 2000, 1000, True, True),
    ("upload", 2000, 1000, None, True), ("download", 1000, 2000, None, True),
    ("upload", 1000, 1000, None, True), ("download", None, 1000, None, True),
])
def test_manual_sync_confirms_older_over_newer_in_both_directions(tmp_path, monkeypatch, direction, local, remote_time, confirm, transferred):
    monkeypatch.setattr(Window, "refresh", lambda _: None)
    w = Window(tmp_path / "data")
    w.monitor.stop()
    w.engine.account_ids = {123}
    w.game = {"game_id": 42, "game_name": "测试"}
    snapshot = {"uid": "uid", "local_time": local, "local_hash": "local",
                "remote": {"save_time": remote_time, "revision": "rev", "content_hash": "remote"}}
    w.cloud.uid = "uid"
    monkeypatch.setattr(w.cloud, "manual_snapshot", lambda *_: snapshot)
    monkeypatch.setattr(w.cloud, "read_state", lambda: {"uid:123:42": {"synced_hash": "remote"}})
    monkeypatch.setattr(w, "task", lambda message, function, done: done(function()))
    prompts, calls = [], []
    def ask(text):
        assert confirm is not None, "newer/equal saves must not prompt"
        prompts.append(text)
        return confirm
    monkeypatch.setattr(w, "confirm", ask)
    monkeypatch.setattr(w, "synced", lambda _: None)
    monkeypatch.setattr(w.cloud, "manual_transfer", lambda *args: calls.append(args))
    if direction == "upload":
        w.upload()
    else:
        monkeypatch.setattr(w.engine, "settings", lambda _: {"paths": ["actual save path"]})
        w.download()
    assert bool(calls) == transferred
    assert bool(prompts) == (confirm is not None)
    if prompts:
        assert "较旧" in prompts[0] and "较新" in prompts[0]
        assert "本地存档时间：" in prompts[0] and "云端存档时间：" in prompts[0]
        assert ("本地存档覆盖云端" if direction == "upload" else "云端存档覆盖本地") in prompts[0]
    if not transferred:
        assert "已取消" in w.status.text()
