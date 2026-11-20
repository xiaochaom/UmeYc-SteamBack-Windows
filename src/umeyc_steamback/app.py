"""中文 Qt 桌面入口；磁盘扫描与网络传输全部在工作线程执行。"""
import os
from pathlib import Path
import sys
import time

import qrcode
from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal, Qt, QLockFile, QUrl
from PySide6.QtGui import QDesktopServices, QPainter, QPixmap, QIcon, QAction
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QInputDialog,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
    QProgressBar, QPushButton, QScrollArea, QSplitter, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget, QAbstractItemView, QSystemTrayIcon, QMenu, QPlainTextEdit,
)

from . import __version__, steam
from .engine import Config, Engine, WindowsSniffer, read_json
from .protocol.cloud import CloudConflict, pack_current
from .protocol.custom import write_json
from .sync import WindowsCloud
from . import desktop
from .launch_options import LaunchOptions
from .ipc import CommandServer, request, server_name
from .updates import check_updates
from .updater import download_update, launch_update, update_in_progress, cleanup_updates, check_other_processes, can_self_update


def date(value):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value / 1000)) if value else "暂无"


def label(text=""):
    widget = QLabel(text)
    widget.setWordWrap(True)
    widget.setTextInteractionFlags(Qt.TextSelectableByMouse)
    return widget


def line():
    widget = QFrame()
    widget.setFrameShape(QFrame.HLine)
    widget.setStyleSheet("color: #526277; margin-top: 10px; margin-bottom: 10px;")
    return widget


def button(text, callback, layout):
    widget = QPushButton(text)
    widget.clicked.connect(callback)
    layout.addWidget(widget)
    return widget


class Result(QObject):
    done = Signal(object, object)
    progress = Signal(int, int)


class Job(QRunnable):
    def __init__(self, function):
        super().__init__()
        self.function = function
        self.signals = Result()

    def run(self):
        try:
            result = self.function()
        except Exception as error:
            self.signals.done.emit(None, error)
        else:
            self.signals.done.emit(result, None)


class PathsDialog(QDialog):
    def __init__(self, paths, parent):
        super().__init__(parent)
        self.setWindowTitle("选择游戏存档路径")
        self.resize(690, 400)
        layout = QVBoxLayout(self)
        layout.addWidget(label("选择具体存档文件或文件夹。多目录的顺序对应云端目录顺序；不会自动备份未选择的路径。"))
        self.items = QListWidget()
        self.items.addItems(paths)
        layout.addWidget(self.items)
        row = QHBoxLayout()
        button("添加文件夹", self.folder, row)
        button("添加文件", self.file, row)
        button("移除", lambda: self.items.takeItem(self.items.currentRow()), row)
        button("上移", lambda: self.move(-1), row)
        button("下移", lambda: self.move(1), row)
        layout.addLayout(row)
        controls = QDialogButtonBox()
        controls.addButton("保存路径", QDialogButtonBox.AcceptRole).clicked.connect(self.accept)
        controls.addButton("取消", QDialogButtonBox.RejectRole).clicked.connect(self.reject)
        layout.addWidget(controls)

    def move(self, delta):
        i = self.items.currentRow()
        if 0 <= i + delta < self.items.count():
            item = self.items.takeItem(i)
            self.items.insertItem(i + delta, item)
            self.items.setCurrentRow(i + delta)

    def folder(self):
        path = QFileDialog.getExistingDirectory(self, "选择具体游戏的存档文件夹")
        if path:
            self.items.addItem(path)

    def file(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "选择存档文件")
        self.items.addItems(paths)

    def paths(self):
        return [self.items.item(i).text() for i in range(self.items.count())]


class Window(QMainWindow):
    def __init__(self, data_dir, backup_root=None):
        super().__init__()
        self.data = Path(data_dir)
        ui = read_json(self.data / "ui.json")
        self.engine = Engine(Config(str(self.data), ui.get("steam", str(steam.steam_root())),
                                    backup_root=str(backup_root) if backup_root is not None else None))
        self.cloud = WindowsCloud(self.engine)
        self.sniffer = WindowsSniffer(self.engine)
        self.items = []
        self.game = None
        self.busy = False
        self.remote = None
        self.auth = {}
        self.last_running = None
        self.pending_auto = []
        self.upload_queue = self.cloud.retry_queue
        self.launch_options = LaunchOptions(self.engine)
        self.launch_requests = []
        self.background_mode = False
        self.page_dirty = False
        self.quitting = False
        self.server = None
        self.tray = None
        self.session_host = False
        self.sessions = {}
        self.last_session_work = time.monotonic()
        self.local_times = {}
        self.update_busy = False
        self.update_downloading = False
        self.installing_update = False
        self.update_info = None
        self.prepared_update = None
        self.update_url = None
        self.update_action = None
        self.update_job = None
        self.update_pool = QThreadPool(self)
        self.update_pool.setMaxThreadCount(1)
        self.setWindowTitle(f"UmeYc-SteamBack-Windows  {__version__}")
        self.resize(1120, 810)
        icon = Path(__file__).parent / "assets/cloud-save.ico"
        self.setWindowIcon(QIcon(str(icon)))
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        self.header = QWidget()
        header = QHBoxLayout(self.header)
        header.setContentsMargins(0, 0, 0, 0)
        self.account_box = QComboBox()
        self.account_box.setMinimumWidth(200)
        self.account_box.currentIndexChanged.connect(self.change_account)
        header.addWidget(self.account_box)
        button("指定 Steam 目录", self.select_steam, header)
        button("填写账号 ID", self.manual_account, header)
        button("刷新游戏", self.refresh, header)
        self.login_button = button("登录夸克", self.login, header)
        button("退出夸克", self.logout, header)
        layout.addWidget(self.header)
        settings_row = QHBoxLayout()
        settings_row.addWidget(label("Steam 启动时按需唤起；上传完成后自动退出，无需开机自启。"))
        button("后台记录", self.show_activity, settings_row)
        layout.addLayout(settings_row)
        update_row = QHBoxLayout()
        self.update_status = label(f"当前版本：{__version__}")
        update_row.addWidget(self.update_status, 1)
        self.check_update_button = button("检查更新", self.check_for_updates, update_row)
        self.download_update_button = button("下载并更新", self.open_update, update_row)
        self.download_update_button.hide()
        layout.addLayout(update_row)
        self.splitter = QSplitter()
        left = QWidget()
        left_layout = QVBoxLayout(left)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索游戏名称 / App ID")
        self.search.textChanged.connect(self.filter_games)
        left_layout.addWidget(self.search)
        self.games_list = QListWidget()
        self.games_list.currentItemChanged.connect(self.choose_game)
        left_layout.addWidget(self.games_list)
        button("添加游戏（填写 Steam App ID）", self.add_game, left_layout)
        self.splitter.addWidget(left)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.page = QWidget()
        page = QVBoxLayout(self.page)
        self.title = label("选择左侧游戏")
        self.title.setStyleSheet("font-size: 21px; font-weight: 600;")
        page.addWidget(self.title)
        self.paths_text = label("支持 Steam 云与非 Steam 云游戏；只操作已选择的存档路径。")
        page.addWidget(self.paths_text)
        path_actions = QHBoxLayout()
        button("选择存档路径", self.choose_paths, path_actions)
        button("识别候选路径", self.detect_paths, path_actions)
        page.addLayout(path_actions)
        sniff_actions = QHBoxLayout()
        button("开始嗅探存档位置", self.begin_sniff, sniff_actions)
        button("游戏保存并退出后，完成嗅探", self.finish_sniff, sniff_actions)
        page.addLayout(sniff_actions)
        launch_options_row = QHBoxLayout()
        button("配置 Steam 启动项", self.configure_launch_options, launch_options_row)
        page.addLayout(launch_options_row)
        self.launch_button = QPushButton("开始游戏")
        self.launch_button.clicked.connect(self.launch_game)
        page.addWidget(self.launch_button)
        page.addWidget(line())
        heading = label("云存档 · 夸克")
        heading.setStyleSheet("font-size: 17px; font-weight: 600;")
        page.addWidget(heading)
        self.times = label("本地存档时间：暂无\n云端存档时间：请刷新云端状态")
        page.addWidget(self.times)
        self.auto_sync = QCheckBox("自动云同步（开始前同步，退出后上传；默认关闭）")
        self.auto_sync.clicked.connect(self.toggle_auto_sync)
        page.addWidget(self.auto_sync)
        page.addWidget(label("开启时配置 Steam 启动项；此后从 Steam 正常启动。后台运行自动上传，进度冲突时暂停并提示。"))
        row = QHBoxLayout()
        button("本地同步到云端", self.upload, row)
        button("手动同步", self.manual_sync, row)
        button("云端同步到本地", self.download, row)
        page.addLayout(row)
        row = QHBoxLayout()
        button("刷新云端状态", self.refresh_cloud, row)
        page.addLayout(row)
        page.addWidget(label("云端每个账号、每个游戏只保留最新一份。覆盖前会确认并保留本地备份。"))
        page.addWidget(line())
        row = QHBoxLayout()
        heading = label("本地存档备份")
        heading.setStyleSheet("font-size: 17px; font-weight: 600;")
        row.addWidget(heading)
        button("备份到本地", self.backup, row)
        page.addLayout(row)
        self.backup_path = label()
        page.addWidget(self.backup_path)
        page.addWidget(label("所有本地备份持续保留。删除备份只删除备份文件，不会删除游戏当前的存档。双击记录可重命名、恢复或删除。"))
        self.records = QTableWidget(0, 3)
        self.records.setHorizontalHeaderLabels(["名称 / 来源", "存档时间", "备份时间"])
        self.records.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.records.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.records.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.records.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.records.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.records.cellDoubleClicked.connect(self.open_record)
        self.records.setMinimumHeight(220)
        page.addWidget(self.records)
        page.addStretch()
        scroll.setWidget(self.page)
        self.splitter.addWidget(scroll)
        self.splitter.setSizes([280, 800])
        layout.addWidget(self.splitter, 1)
        self.status = label("就绪")
        layout.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setVisible(False)
        self.progress.setMaximumHeight(5)
        layout.addWidget(self.progress)
        self.monitor = QTimer(self)
        self.monitor.setInterval(3000)
        self.monitor.timeout.connect(self.monitor_games)
        self.monitor.start()
        self.command_timer = QTimer(self)
        self.command_timer.setInterval(500)
        self.command_timer.timeout.connect(self.process_launch_requests)
        self.load_accounts(ui.get("account"))
        QTimer.singleShot(0, self.refresh)

    def setup_background(self):
        self.server = CommandServer(server_name(self.data), self)
        self.server.command.connect(self.handle_command)
        self.command_timer.start()
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(self.windowIcon(), self)
            self.tray.setToolTip("UmeYc-SteamBack：后台监听中")
            menu = QMenu(self)
            show = menu.addAction("打开存档管理")
            show.triggered.connect(self.show_window)
            activity = menu.addAction("查看后台同步记录")
            activity.triggered.connect(lambda: (self.show_window(), self.show_activity()))
            self.update_action = menu.addAction("检查更新")
            self.update_action.triggered.connect(lambda: (self.show_window(), self.check_for_updates()))
            menu.addSeparator()
            quit_action = menu.addAction("退出程序（停止监听）")
            quit_action.triggered.connect(self.quit_app)
            self.tray.setContextMenu(menu)
            self.tray.activated.connect(lambda reason: self.show_window() if reason == QSystemTrayIcon.DoubleClick else None)
            self.tray.show()

    def check_for_updates(self):
        # Separate worker pool and state: an unavailable GitHub connection must
        # never occupy the serialized game-save/sync task or block launch IPC.
        if self.update_busy or self.update_downloading or self.installing_update or self.quitting:
            return
        self.update_busy = True
        self.check_update_button.setEnabled(False)
        self.download_update_button.setEnabled(False)
        self.update_status.setText(f"当前版本：{__version__} · 正在检查更新…")
        job = Job(check_updates)
        self.update_job = job
        def complete(result, error):
            self.update_busy = False
            self.check_update_button.setEnabled(True)
            self.download_update_button.setEnabled(True)
            if error:
                message = str(error) if isinstance(error, ValueError) else "检查更新失败，请稍后重试"
                self.update_status.setText(f"当前版本：{__version__} · {message}")
                return  # Keep any previously discovered download available.
            self.update_url = result.url if result.available else None
            self.update_info = result if result.available else None
            self.download_update_button.setVisible(result.available)
            self.update_status.setText(f"当前版本：{__version__} · 发现新版 {result.version}" if result.available
                                       else f"当前版本：{__version__} · 已是最新版本")
            if self.update_action:
                self.update_action.setText(f"下载并更新 {result.version}" if result.available else "检查更新")
                self.update_action.triggered.disconnect()
                self.update_action.triggered.connect(self.open_update if result.available else
                    lambda: (self.show_window(), self.check_for_updates()))
        job.signals.done.connect(complete)
        self.update_pool.start(job)

    def open_update(self):
        self.show_window()
        if self.update_busy or self.update_downloading or self.installing_update or self.quitting:
            return
        if self.prepared_update:
            self.install_prepared_update()
            return
        if not self.update_info:
            return
        if not can_self_update():
            self.update_status.setText("应用内更新仅支持 Windows 打包版本；源码运行请使用 Git 更新")
            return
        result = self.update_info
        self.update_downloading = True
        self.check_update_button.setEnabled(False)
        self.download_update_button.setEnabled(False)
        self.update_status.setText(f"正在下载 {result.version}，完成后将重启更新…")
        job = Job(lambda: download_update(result, self.data, job.signals.progress.emit))
        self.update_job = job
        def progress(received, total):
            detail = f"{received * 100 // total}%" if total else f"{received // 1024**2} MB"
            self.update_status.setText(f"正在下载 {result.version}：{detail}（下载后校验）")
        def complete(prepared, error):
            self.update_downloading = False
            self.check_update_button.setEnabled(True)
            self.download_update_button.setEnabled(True)
            if error:
                message = str(error) if isinstance(error, (ValueError, OSError)) else "下载更新失败，请重试"
                self.update_status.setText(message)
                return
            self.prepared_update = prepared
            self.download_update_button.setText("重启并更新")
            self.install_prepared_update()
        job.signals.progress.connect(progress)
        job.signals.done.connect(complete)
        self.update_pool.start(job)

    def install_prepared_update(self):
        if self.installing_update or not self.prepared_update:
            return
        if (self.busy or self.launch_requests or self.sessions or self.cloud.running
                or steam.running_games(self.items)):
            self.update_status.setText("更新已下载并校验；请退出游戏、等待存档同步完成后点“重启并更新”")
            return
        try:
            check_other_processes(desktop.app_directory())
        except ValueError as error:
            self.update_status.setText(str(error))
            return
        self.installing_update = True
        self.monitor.stop()
        self.command_timer.stop()
        self.header.setEnabled(False)
        self.splitter.setEnabled(False)
        self.check_update_button.setEnabled(False)
        self.download_update_button.setEnabled(False)
        self.update_status.setText("正在准备重启更新，配置和备份将保留…")
        job = Job(lambda: launch_update(self.prepared_update, desktop.app_directory()))
        self.update_job = job
        def complete(_, error):
            if error:
                self.installing_update = False
                self.monitor.start()
                if self.server:
                    self.command_timer.start()
                self.header.setEnabled(True)
                self.splitter.setEnabled(True)
                self.check_update_button.setEnabled(True)
                self.download_update_button.setEnabled(True)
                self.update_status.setText(str(error))
                return
            self.quitting = True
            if self.tray:
                self.tray.hide()
            QApplication.instance().quit()
        job.signals.done.connect(complete)
        self.update_pool.start(job)

    def show_window(self):
        self.session_host = False  # Explicitly opened management stays available.
        self.background_mode = False
        self.centralWidget().setUpdatesEnabled(True)
        self.monitor.setInterval(3000)
        self.showNormal()
        self.raise_()
        self.activateWindow()
        if self.page_dirty:
            self.page_dirty = False
            self.render_game()

    def hide_window(self):
        self.background_mode = True
        self.hide()
        self.centralWidget().setUpdatesEnabled(False)
        self.monitor.setInterval(5000)

    def changeEvent(self, event):
        super().changeEvent(event)
        from PySide6.QtCore import QEvent
        if event.type() == QEvent.WindowStateChange and self.isMinimized() and self.tray:
            QTimer.singleShot(0, self.hide_window)

    def quit_app(self):
        if self.update_downloading or self.installing_update:
            self.show_window()
            self.update_status.setText("正在下载或准备更新，请完成后再退出")
            return
        if self.busy or self.launch_requests:
            self.show_window()
            self.status.setText("正在同步存档，请完成后再退出；关闭窗口仍可在后台继续")
            return
        self.quitting = True
        self.monitor.stop()
        self.command_timer.stop()
        if self.tray:
            self.tray.hide()
        QApplication.instance().quit()

    def handle_command(self, command, reply):
        if self.installing_update:
            reply({"ok": False, "error": "工具正在更新，请完成后再启动游戏"})
            return
        if command["action"] == "show":
            self.show_window()
            self.check_for_updates()
            reply({"ok": True})
            return
        if len(self.launch_requests) >= 10:
            reply({"ok": False, "error": "等待同步的启动请求过多"})
            return
        self.launch_requests.append((command, reply))
        self.process_launch_requests()

    def process_launch_requests(self):
        if self.busy or not self.launch_requests:
            return
        command, reply = self.launch_requests.pop(0)
        account, aid = command["account"], command["game_id"]
        self.last_session_work = time.monotonic()
        try:
            desktop.check_steam_account(account)
            if self.engine.account_ids != {account}:
                raise ValueError("监听账号与 Steam 游戏账号不同，请打开工具选择正确账号")
            games = {g["game_id"]: g for g in self.items}
            for game in self.engine.configured_games():
                games.setdefault(game["game_id"], game)
            if aid not in games:
                raise ValueError("请先在工具里配置此游戏的存档路径")
            game = games[aid]
            if command["action"] == "finished":
                self.sessions.pop((account, aid), None)
                # Registry can remain running until the wrapper exits. Let
                # observe() wait for it to clear before scheduling the upload.
                if self.cloud.uid and self.cloud.read_state().get(self.cloud.key(account, game), {}).get("auto_upload"):
                    self.upload_queue.previous = ((self.cloud.uid, account), {aid} | (self.upload_queue.previous or (None, set()))[1])
                reply({"ok": True})
                return
        except (ValueError, OSError) as error:
            reply({"ok": False, "error": str(error)})
            return
        if (account, aid) in self.sessions:
            reply({"ok": False, "error": "此游戏已存在运行中的同步会话，请退出后再启动"})
            return
        def done(result):
            self.sessions[(account, aid)] = time.monotonic()
            self.page_dirty = True
            self.status.setText(result["message"])
            reply({"ok": True})
        def failed(error):
            text = str(error) if isinstance(error, (ValueError, OSError)) else "启动前同步失败，请打开工具检查"
            self.status.setText(text)
            reply({"ok": False, "error": text})
        self.task("正在执行 Steam 启动前云同步…", lambda: self.cloud.prepare_steam_launch(account, game), done, failed, quiet=True)

    def task(self, message, function, done=None, failed=None, quiet=False):
        if self.busy or self.installing_update:
            return
        self.busy = True
        self.header.setEnabled(False)
        self.splitter.setEnabled(False)
        self.progress.setVisible(not quiet)
        if not quiet:
            self.status.setText(message)
        job = Job(function)
        self.current_job = job

        def complete(value, error):
            self.busy = False
            self.header.setEnabled(True)
            self.splitter.setEnabled(True)
            self.progress.hide()
            if error:
                if failed:
                    failed(error)
                else:
                    self.error(error)
            elif done:
                try:
                    done(value)
                except Exception as callback_error:
                    self.error(callback_error)
            elif not quiet:
                self.status.setText("操作完成")

        job.signals.done.connect(complete)
        QThreadPool.globalInstance().start(job)

    def error(self, error):
        text = str(error) if isinstance(error, (ValueError, OSError)) else f"操作失败（{type(error).__name__}），请重试"
        self.status.setText(text)
        if not self.background_mode:
            QMessageBox.warning(self, "未完成操作", text)

    def confirm(self, text):
        dialog = QMessageBox(self)
        dialog.setWindowTitle("确认操作")
        dialog.setText(text)
        yes = dialog.addButton("确认", QMessageBox.AcceptRole)
        dialog.addButton("取消", QMessageBox.RejectRole)
        dialog.exec()
        return dialog.clickedButton() == yes

    def save_ui(self):
        write_json(self.data / "ui.json", {"steam": self.engine.config.steam_dir,
                                          "account": next(iter(self.engine.account_ids), None)})

    def load_accounts(self, selected=None):
        self.account_box.blockSignals(True)
        self.account_box.clear()
        items = steam.accounts(Path(self.engine.get_steam_root()))
        if selected and not any(a["id"] == selected for a in items):
            items.append({"id": selected, "name": "Steam 用户"})
        for a in items:
            self.account_box.addItem(a["name"], a["id"])
        index = self.account_box.findData(selected)
        self.account_box.setCurrentIndex(max(0, index))
        self.account_box.blockSignals(False)
        self.change_account()

    def change_account(self):
        account = self.account_box.currentData()
        self.engine.account_ids = {account} if account else set()
        self.cloud.active_account = account
        self.last_running = None
        self.upload_queue.previous = None
        self.pending_auto.clear()
        self.game = None
        self.remote = None
        self.render_game()
        self.save_ui()
        if account:
            QTimer.singleShot(0, self.refresh)

    def manual_account(self):
        text, ok = QInputDialog.getText(self, "Steam 账号", "输入 Steam Account ID（32 位），也可填写 SteamID64：")
        if ok:
            try:
                account = int(text)
                if account > steam.STEAM_ID_BASE:
                    account -= steam.STEAM_ID_BASE
                if not 0 < account < 2**32:
                    raise ValueError("Steam 账号 ID 无效")
                self.load_accounts(account)
            except ValueError as error:
                self.error(error)

    def select_steam(self):
        path = QFileDialog.getExistingDirectory(self, "选择 Steam 根目录", self.engine.get_steam_root())
        if path:
            self.engine.config.steam_dir = path
            self.load_accounts()
            self.refresh()

    def refresh(self):
        if self.busy:
            return
        def work():
            games = {g["game_id"]: g for g in steam.games(Path(self.engine.get_steam_root()))}
            if self.engine.account_ids:
                for g in self.engine.configured_games():
                    games.setdefault(g["game_id"], g)
            return list(games.values()), self.cloud.account_status()
        def show(result):
            self.items, self.auth = result
            self.update_auth()
            self.filter_games()
            self.status.setText(f"已加载 {len(self.items)} 个游戏。请选择 Steam 账号和游戏。")
        self.task("正在读取 Steam 游戏与夸克登录状态…", work, show)

    def update_auth(self):
        a = self.auth
        self.login_button.setText((a.get("nickname") or "夸克用户") if a.get("logged_in") else "登录夸克")
        self.login_button.setToolTip(a.get("error", "扫码登录夸克"))

    def filter_games(self):
        selected = self.game["game_id"] if self.game else None
        text = self.search.text().casefold()
        self.games_list.blockSignals(True)
        self.games_list.clear()
        entries = [g for g in self.items if not steam.is_compatibility_tool(g["game_name"])
                   and text in f"{g['game_name']} {g['game_id']}".casefold()]
        for game in sorted(entries, key=lambda g: g["game_name"].casefold()):
            item = QListWidgetItem(f"{game['game_name']}\nApp ID {game['game_id']}")
            item.setData(Qt.UserRole, game)
            self.games_list.addItem(item)
            if game["game_id"] == selected:
                self.games_list.setCurrentItem(item)
        if not self.games_list.currentItem():
            first = next((i for i in range(self.games_list.count()) if self.games_list.item(i).data(Qt.UserRole)), None)
            if first is not None:
                self.games_list.setCurrentRow(first)
        self.games_list.blockSignals(False)
        self.choose_game(self.games_list.currentItem())

    def choose_game(self, item, previous=None):
        self.game = item.data(Qt.UserRole) if item else None
        self.remote = None
        self.render_game()

    def require_game(self):
        if not self.game or not self.engine.account_ids:
            self.error(ValueError("请先选择 Steam 账号和游戏"))
            return False
        return True

    def render_game(self):
        if self.background_mode:
            self.page_dirty = True
            return
        self.page.setEnabled(bool(self.game and self.engine.account_ids))
        if not self.game:
            self.title.setText("选择左侧游戏")
            return
        game = self.game
        settings = self.engine.settings(game) if self.engine.account_ids else {}
        self.title.setText(f"{game['game_name']}  ·  {game['game_id']}")
        paths = settings.get("paths", [])
        keys = settings.get("keys")
        self.paths_text.setText("当前存档路径：\n" + ("\n".join(
            f"{i+1}. {p}" + (f"\n    ↔ {keys[i]}" if keys and i < len(keys) else "") for i, p in enumerate(paths)) or "尚未选择；可识别候选、手动选择或嗅探"))
        if not self.engine.account_ids:
            return
        try:
            self.backup_path.setText(f"备份保存路径：{self.engine.backup_dir(game)}")
            records = self.engine.backups(game)
        except (OSError, ValueError) as error:
            self.records.setRowCount(0)
            self.error(error)
            return
        self.render_times()
        account = self.engine.account
        selected_game = dict(game)
        def update_local(value):
            self.local_times[(account, selected_game["game_id"])] = value
            if self.game and self.game["game_id"] == selected_game["game_id"] and self.engine.account == account:
                self.render_times()
        def read_local():
            if not self.background_mode and not self.busy and QApplication.activeModalWidget() is None:
                self.task("", lambda: self.cloud.isolated_engine(account).save_time(selected_game), update_local, quiet=True)
        QTimer.singleShot(0, read_local)
        self.auto_sync.setChecked(self.cloud.read_state().get(self.cloud.key(self.engine.account, game), {}).get("auto_sync", False))
        self.records.setRowCount(len(records))
        sources = {"manual": "手动备份", "cloud_download": "云端下载", "before_cloud_sync": "同步前本地备份", "before_restore": "恢复前本地备份"}
        for i, record in enumerate(records):
            for j, text in enumerate([record.get("name") or sources.get(record["source"], "本地备份"), date(record.get("save_time")), date(record["time"])]):
                item = QTableWidgetItem(text)
                item.setData(Qt.UserRole, record)
                self.records.setItem(i, j, item)

    def render_times(self):
        local = self.local_times.get((self.engine.account, self.game["game_id"]))
        self.times.setText(f"本地存档时间：{date(local)}\n云端存档时间：{date((self.remote or {}).get('save_time'))}"
                           + f"\n云端上传时间：{date((self.remote or {}).get('uploaded_at'))}")

    def choose_paths(self):
        if not self.require_game():
            return
        dialog = PathsDialog(self.engine.settings(self.game).get("paths", []), self)
        if dialog.exec() == QDialog.Accepted:
            try:
                self.engine.configure(self.game, dialog.paths())
                self.remote = None
                self.render_game()
            except (ValueError, OSError) as error:
                self.error(error)

    def add_game(self):
        if not self.engine.account_ids:
            return self.error(ValueError("请先选择或填写 Steam 账号 ID"))
        aid, ok = QInputDialog.getText(self, "添加游戏", "游戏的 Steam App ID（两台机器必须一致）：")
        if not ok:
            return
        if not aid.isdigit() or not 0 < int(aid) < 2**32:
            return self.error(ValueError("请输入有效的 Steam App ID"))
        name, ok = QInputDialog.getText(self, "添加游戏", "游戏名称：")
        if ok and name.strip():
            game = {"game_id": int(aid), "game_name": name.strip(), "install_root": self.engine.get_steam_root(), "install_dir": ""}
            self.items = [g for g in self.items if g["game_id"] != game["game_id"]] + [game]
            self.game = game
            self.filter_games()
            self.choose_paths()

    def detect_paths(self):
        if self.require_game():
            game, account = dict(self.game), self.engine.account
            self.task("正在查找常见存档目录…", lambda: steam.candidates(Path(self.engine.get_steam_root()), account, game),
                      lambda paths: self.select_candidates(paths, "识别到的候选路径"))

    def select_candidates(self, paths, title, warning=""):
        if not paths:
            self.status.setText("未找到候选存档。可手动选择，或开始嗅探后运行游戏并保存。" + warning)
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.resize(800, 450)
        layout = QVBoxLayout(dialog)
        layout.addWidget(label("候选可能含配置或缓存，请勾选实际存档。" + warning))
        items = QListWidget()
        for p in paths:
            item = QListWidgetItem(p)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            items.addItem(item)
        layout.addWidget(items)
        controls = QDialogButtonBox()
        controls.addButton("采用勾选路径", QDialogButtonBox.AcceptRole).clicked.connect(dialog.accept)
        controls.addButton("取消", QDialogButtonBox.RejectRole).clicked.connect(dialog.reject)
        layout.addWidget(controls)
        if dialog.exec() == QDialog.Accepted:
            selected = [items.item(i).text() for i in range(items.count()) if items.item(i).checkState() == Qt.Checked]
            self.engine.configure(self.game, selected)
            self.render_game()

    def begin_sniff(self):
        if self.require_game():
            game, account = dict(self.game), self.engine.account
            self.task("正在记录当前文件状态…", lambda: self.sniffer.begin(game, account),
                      lambda result: self.status.setText(result["message"] + ("（扫描达到限制，结果可能不完整）" if result["truncated"] else "")))

    def finish_sniff(self):
        if self.require_game():
            game, account = dict(self.game), self.engine.account
            self.task("正在对比变更文件…", lambda: self.sniffer.finish(game, account),
                      lambda result: self.select_candidates([r["path"] for r in result["candidates"]], "嗅探到的变更文件",
                                                          "扫描达到限制，结果可能不完整。" if result["truncated"] else ""))

    def refresh_cloud(self):
        if not self.require_game():
            return
        game, account = dict(self.game), self.engine.account
        def work():
            self.cloud.uid = self.cloud.client.account()["user_id"]
            return self.cloud.latest(account, game)
        def show(remote):
            self.remote = remote
            self.render_game()
            self.status.setText("云端状态已刷新" if remote else "云端暂无此游戏的存档")
        self.task("正在读取云端存档时间…", work, show)

    def toggle_auto_sync(self, enabled):
        if self.require_game():
            game, account = dict(self.game), self.engine.account
            def save():
                result = self.cloud.set_auto_sync(account, game, enabled)
                if not enabled:
                    self.upload_queue.clear(self.cloud.uid, account, game["game_id"])
                return result
            def done(_):
                self.render_game()
                if enabled:
                    self.status.setText("自动云同步已开启，配置启动项后会按需运行；正常同步不弹窗")
                    if not self.launch_options.installed(account, game["game_id"]):
                        self.configure_launch_options()
            self.task("正在保存自动云同步设置…", save, done,
                      lambda e: (self.render_game(), self.error(e)))

    def launch_game(self):
        if not self.require_game():
            return
        game, account = dict(self.game), self.engine.account
        # Steam launch options own the preflight check when configured. This
        # button requests the same normal Steam entry; no duplicate download.
        if not QDesktopServices.openUrl(QUrl(f"steam://rungameid/{int(game['game_id'])}")):
            self.error(ValueError("无法打开 Steam"))

    def configure_launch_options(self, checked=False):
        if not self.require_game():
            return
        account, game = self.engine.account, dict(self.game)
        try:
            old, generated = self.launch_options.preview(account, game["game_id"])
            self.launch_options.remember_preview(account, game["game_id"], generated)
        except (ValueError, OSError) as error:
            return self.error(error)
        dialog = QDialog(self)
        dialog.setWindowTitle("配置 Steam 启动项")
        dialog.resize(760, 380)
        layout = QVBoxLayout(dialog)
        layout.addWidget(label("无需重启 Steam：复制下方内容，打开游戏属性，粘贴到“启动选项”。只增加同步入口，已保留原参数。非 Steam 快捷游戏不适用此方式。"))
        layout.addWidget(label("原启动选项：" + (old or "（空）")))
        content = QPlainTextEdit(generated)
        content.setReadOnly(True)
        layout.addWidget(content)
        row = QHBoxLayout()
        button("复制启动项", lambda: QApplication.clipboard().setText(generated), row)
        button("打开游戏属性", lambda: QDesktopServices.openUrl(QUrl(f"steam://gameproperties/{game['game_id']}")), row)
        layout.addLayout(row)
        layout.addWidget(label("也可以先手动退出 Steam，再点“直接写入”。写入前自动备份配置；不会强制退出或重启 Steam。移动程序目录后需重新配置。关闭自动云同步后会跳过同步并正常启动。"))
        row = QHBoxLayout()
        def apply():
            dialog.accept()
            self.task("正在备份并写入启动项…", lambda: self.launch_options.update(account, game["game_id"]))
        button("Steam 已退出，直接写入", apply, row)
        button("关闭", dialog.reject, row)
        layout.addLayout(row)
        dialog.exec()

    def show_activity(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("后台同步记录")
        dialog.resize(680, 400)
        layout = QVBoxLayout(dialog)
        records = read_json(self.upload_queue.activity)
        view = QPlainTextEdit()
        view.setReadOnly(True)
        view.setPlainText("\n\n".join(f"{date(r['time'])} · {r['game_name']}\n{r['message']}"
                                    for r in sorted(records.values(), key=lambda r: r["time"], reverse=True)) or "暂无后台同步记录")
        layout.addWidget(view)
        button("关闭", dialog.accept, layout)
        dialog.exec()

    def manual_sync(self):
        if not self.require_game():
            return
        game, account = dict(self.game), self.engine.account
        def inspect():
            self.cloud.isolated_engine(account).guard(game)
            self.cloud.uid = self.cloud.client.account()["user_id"]
            return self.cloud.latest(account, game), self.cloud.isolated_engine(account).save_time(game)
        def show(result):
            if self.engine.account != account or not self.game or self.game["game_id"] != game["game_id"]:
                return
            self.remote, local = result
            self.local_times[(account, game["game_id"])] = local
            self.render_times()
            self.manual_sync_dialog(game, account, self.remote)
        self.task("正在读取两端存档时间…", inspect, show)

    def manual_sync_dialog(self, game, account, remote):
        dialog = QDialog(self)
        dialog.setWindowTitle("手动同步：选择要使用的存档")
        dialog.resize(520, 230)
        layout = QVBoxLayout(dialog)
        layout.addWidget(label(f"本地存档时间：{date(self.local_times.get((account, game['game_id'])))}\n云端存档时间：{date((remote or {}).get('save_time'))}\n请选择较新的进度。使用本地会上传并替换云端版本；使用云端会下载并覆盖当前配置路径。"))
        row = QHBoxLayout()
        use_local = button("使用本地存档上传", lambda: (dialog.accept(), self.upload()), row)
        use_cloud = button("使用云端存档下载", lambda: (dialog.accept(), self.download()), row)
        use_cloud.setEnabled(bool(remote))
        button("关闭", dialog.reject, row)
        layout.addLayout(row)
        dialog.exec()

    def upload(self):
        if not self.require_game():
            return
        game, account = dict(self.game), self.engine.account
        self.start_manual_transfer(account, game, "upload")

    def start_manual_transfer(self, account, game, direction):
        def inspected(snapshot):
            if self.engine.account_ids != {account} or not self.game or self.game["game_id"] != game["game_id"]:
                return
            remote = snapshot["remote"]
            if direction == "download" and not remote:
                return self.error(ValueError("云端暂无存档，请先从另一台设备上传"))
            local_time, cloud_time = snapshot["local_time"], (remote or {}).get("save_time")
            source, target = (local_time, cloud_time) if direction == "upload" else (cloud_time, local_time)
            older = source is not None and target is not None and source < target
            state = self.cloud.read_state().get(self.cloud.key(account, game), {})
            conflict = (direction == "upload" and remote and remote["content_hash"] != snapshot["local_hash"]
                        and remote["content_hash"] != state.get("synced_hash"))
            if older or conflict:
                action = "用本地存档覆盖云端存档" if direction == "upload" else "用云端存档覆盖本地存档"
                reason = "即将用较旧的存档覆盖较新的存档。" if older else "云端存在尚未同步的不同进度。"
                if not self.confirm(f"{reason}\n本地存档时间：{date(local_time)}\n云端存档时间：{date(cloud_time)}\n确定{action}？"):
                    self.status.setText("已取消同步，存档未修改")
                    return
            self.task("正在上传云存档…" if direction == "upload" else "正在下载并覆盖存档，覆盖前保存本地备份…",
                      lambda: self.cloud.manual_transfer(account, game, direction, snapshot), self.synced)
        self.task("正在核对本地和云端存档时间…", lambda: self.cloud.manual_snapshot(account, game), inspected)

    def synced(self, result):
        self.status.setText(result["message"])
        self.remote = None
        self.render_game()

    def download(self):
        if not self.require_game():
            return
        game, account = dict(self.game), self.engine.account
        if not self.engine.settings(game).get("paths"):
            dialog = PathsDialog([], self)
            dialog.setWindowTitle("选择游戏实际存档路径")
            if dialog.exec() != QDialog.Accepted:
                return
            try:
                self.engine.configure(game, dialog.paths())
            except (ValueError, OSError) as error:
                return self.error(error)
        # The user chose cloud -> local. Reuse the actual game paths and stage
        # the archive privately; no second per-directory mapping dialog.
        self.start_manual_transfer(account, game, "download")

    def backup(self):
        if self.require_game():
            game = dict(self.game)
            self.task("正在备份到本地…", lambda: self.engine.backup(game), lambda _: (self.render_game(), self.status.setText("本地备份已保存")))

    def open_record(self, row, column=0):
        game = dict(self.game)
        record = self.records.item(row, 0).data(Qt.UserRole)
        dialog = QDialog(self)
        dialog.setWindowTitle("管理本地备份")
        dialog.resize(530, 200)
        layout = QVBoxLayout(dialog)
        layout.addWidget(label(f"存档时间：{date(record.get('save_time'))}　备份时间：{date(record['time'])}"))
        name = QLineEdit(record.get("name", ""))
        name.setPlaceholderText("备份名称")
        name.setMaxLength(100)
        layout.addWidget(name)
        grid = QGridLayout()
        restore, delete, save, close = [QPushButton(t) for t in ["恢复此备份", "删除此备份", "保存名称", "关闭"]]
        for widget, r, c in [(restore, 0, 0), (delete, 0, 1), (save, 1, 0), (close, 1, 1)]:
            grid.addWidget(widget, r, c)
        layout.addLayout(grid)
        def rename():
            try:
                self.engine.rename(game, record["id"], name.text())
            except (ValueError, OSError) as error:
                return self.error(error)
            dialog.accept()
            self.render_game()
        def remove():
            if self.confirm("只删除这份备份，不删除游戏当前的存档。确定删除？"):
                try:
                    self.engine.delete(game, record["id"])
                except (ValueError, OSError) as error:
                    return self.error(error)
                dialog.accept()
                self.render_game()
        def apply():
            if self.confirm("将恢复这份备份，并保存恢复前的本地存档。请确认游戏已退出。"):
                dialog.accept()
                self.task("正在恢复本地备份…", lambda: self.engine.restore(game, record["id"]),
                          lambda _: (self.render_game(), self.status.setText("备份已恢复")))
        save.clicked.connect(rename)
        delete.clicked.connect(remove)
        restore.clicked.connect(apply)
        close.clicked.connect(dialog.reject)
        dialog.exec()

    def login(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("使用夸克 App 扫码登录")
        dialog.setFixedSize(350, 410)
        layout = QVBoxLayout(dialog)
        image = QLabel("正在生成二维码…")
        image.setAlignment(Qt.AlignCenter)
        layout.addWidget(image)
        state = label("请使用夸克 App 扫码并确认登录；二维码有效期 5 分钟。")
        layout.addWidget(state)
        button("关闭", dialog.reject, layout)
        session = {}
        poll_timer = QTimer(dialog)
        poll_timer.setInterval(2000)
        def show(qr):
            session.update(qr)
            matrix = qrcode.QRCode(border=4)
            matrix.add_data(qr["url"])
            matrix.make(fit=True)
            values = matrix.get_matrix()
            scale = max(1, 290 // len(values))
            pixmap = QPixmap(len(values)*scale, len(values)*scale)
            pixmap.fill(Qt.white)
            painter = QPainter(pixmap)
            for y, row in enumerate(values):
                for x, dark in enumerate(row):
                    if dark:
                        painter.fillRect(x*scale, y*scale, scale, scale, Qt.black)
            painter.end()
            image.setPixmap(pixmap)
            if dialog.isVisible():
                poll_timer.start()
        def update(result):
            if result["state"] == "logged_in":
                self.auth = result
                self.cloud.uid = result["user_id"]
                self.update_auth()
                poll_timer.stop()
                dialog.accept()
                self.status.setText("夸克登录成功")
            elif result["state"] in ("expired", "cancelled"):
                poll_timer.stop()
                image.clear()
                state.setText("二维码已过期或取消，请关闭后重新登录")
        def poll():
            if not self.busy and session and dialog.isVisible():
                self.task("", lambda: self.cloud.client.poll_login(session["session_id"]), update,
                          lambda e: (poll_timer.stop(), state.setText(str(e))), quiet=True)
        poll_timer.timeout.connect(poll)
        QTimer.singleShot(0, lambda: self.task("正在创建登录二维码…", self.cloud.client.begin_login, show,
                                              lambda e: state.setText(str(e))))
        dialog.exec()
        poll_timer.stop()
        # A pending HTTP call may finish after closing; cancel only when idle.
        if session and not self.busy:
            self.cloud.client.cancel_login(session["session_id"])

    def logout(self):
        def show(result):
            self.auth = result
            self.cloud.uid = None
            self.update_auth()
            self.render_game()
            self.status.setText("已退出夸克，云端与本地备份均保留")
        self.task("正在退出夸克…", self.cloud.client.logout, show)

    def monitor_games(self):
        if self.busy or self.installing_update or self.launch_requests or not self.engine.account_ids:
            return
        account = self.engine.account
        self.maybe_exit_session()
        state = self.cloud.read_state()
        items = [game for game in self.items if state.get(self.cloud.key(account, game), {}).get("auto_upload")]
        if not items:
            return
        uid = self.cloud.uid
        def observe():
            desktop.check_steam_account(account)
            running = steam.running_games(items)
            self.upload_queue.observe(uid, account, items, running, baselines={
                g["game_id"]: state.get(self.cloud.key(account, g), {}).get("synced_hash") for g in items})
            return running, self.upload_queue.next(uid, account, running)
        def observed(result):
            running, pending = result
            self.cloud.running = {(account, aid) for aid in running}
            now = time.monotonic()
            for key, started in list(self.sessions.items()):
                if key[0] == account and key[1] not in running and now - started > 90:
                    # Recover if the Steam wrapper was killed before reporting
                    # its exit. Upload intents are already persisted by observe.
                    self.sessions.pop(key, None)
            if pending:
                def upload():
                    try:
                        result = self.cloud.upload_after_exit(account, pending["game"], uid, base_hash=pending.get("base_hash"))
                    except Exception as error:
                        self.upload_queue.finish(pending, error, conflict=isinstance(error, CloudConflict))
                        raise
                    self.upload_queue.finish(pending)
                    return result
                self.task("正在后台上传云存档…", upload, self.synced,
                          lambda e: self.background_error(e), quiet=True)
        self.task("", observe, observed, lambda e: self.status.setText(str(e)), quiet=True)

    def background_error(self, error):
        conflict = isinstance(error, CloudConflict)
        message = ("云端存在不同进度，已暂停补传，请手动选择同步方向" if conflict
                   else f"后台上传稍后重试：{error}")
        self.status.setText(message + "（详见后台记录）")
        if self.tray:
            self.tray.setToolTip("UmeYc-SteamBack：" + message)

    def maybe_exit_session(self):
        if (not self.session_host or self.busy or self.update_downloading or self.installing_update or self.launch_requests or self.sessions or self.cloud.running
                or any(not p.get("blocked") for p in self.upload_queue.pending.values()) or time.monotonic() - self.last_session_work < 20):
            return
        self.quit_app()

    def closeEvent(self, event):
        if self.tray and not self.quitting:
            self.hide_window()
            event.ignore()
            return
        if self.busy or self.update_downloading or self.installing_update:
            self.status.setText("正在处理存档，请等待当前操作完成后退出")
            event.ignore()
        else:
            event.accept()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("UmeYc-SteamBack-Windows")
    app.setOrganizationName("xiaochaom")
    app.setWindowIcon(QIcon(str(Path(__file__).parent / "assets/cloud-save.ico")))
    if os.name == "nt":
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("xiaochaom.UmeYc.SteamBack.Windows")
    smoke_temp = None
    if "--smoke-test" in sys.argv:
        import tempfile
        smoke_temp = tempfile.TemporaryDirectory()
        data = Path(smoke_temp.name) / "data"
        write_json(data / "ui.json", {"steam": str(Path(smoke_temp.name) / "Steam")})
    else:
        from .storage import data_directory
        data = data_directory()
    if update_in_progress(data):
        QMessageBox.information(None, "正在更新", "工具正在替换程序文件，请等待更新完成后再打开。")
        return 1
    try:
        data.mkdir(parents=True, exist_ok=True)
    except OSError:
        QMessageBox.critical(None, "无法保存数据", "程序目录不可写，请将完整程序文件夹移动到可写位置后重试。")
        return 1
    lock = QLockFile(str(data / ".application.lock"))
    if not lock.tryLock(100):
        if lock.error() == QLockFile.PermissionError:
            QMessageBox.critical(None, "无法保存数据", "程序目录不可写，请将完整程序文件夹移动到可写位置后重试。")
            return 1
        if "--background" not in sys.argv:
            try:
                result = request(server_name(data), {"action": "show"})
                if not result:
                    QMessageBox.information(None, "UmeYc-SteamBack-Windows", "后台正在启动，请稍后打开")
            except (ValueError, OSError):
                pass
        return
    app.setStyle("Fusion")
    from .desktop import app_directory
    backup_root = Path(smoke_temp.name) / "backups" if smoke_temp else app_directory() / "backups"
    if not smoke_temp:
        from .storage import initialize_storage
        try:
            initialize_storage(data, backup_root)
        except (ValueError, OSError) as error:
            QMessageBox.critical(None, "数据迁移未完成", f"{error}\n请确认程序目录可写且空间充足；工具未加载旧配置。")
            lock.unlock()
            return 1
    window = Window(data, backup_root=backup_root)
    window.session_host = "--session-host" in sys.argv
    if not smoke_temp:
        result_file = data / "updates/result.json"
        result = read_json(result_file)
        result_file.unlink(missing_ok=True)
        if result and not result.get("ok"):
            QTimer.singleShot(0, lambda: window.error(ValueError("更新未完成，已恢复旧版程序；配置和备份未修改，请重试更新。")))
        QTimer.singleShot(5000, lambda: cleanup_updates(data))
        QTimer.singleShot(0, window.check_for_updates)
        try:
            window.launch_options.recover()
            window.setup_background()
        except (ValueError, OSError) as error:
            window.error(error)
    if "--background" in sys.argv and (window.tray or window.session_host):
        window.hide_window()
    else:
        window.show()
    if smoke_temp:
        window.monitor.stop()
        QTimer.singleShot(2000, app.quit)
    app.setQuitOnLastWindowClosed(not bool(window.tray))
    app.exec()
    window.update_pool.waitForDone()
    window.monitor.stop()
    window.command_timer.stop()
    if window.tray:
        window.tray.hide()
    lock.unlock()
    if smoke_temp:
        smoke_temp.cleanup()
