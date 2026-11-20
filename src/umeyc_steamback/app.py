"""中文 Qt 桌面入口；磁盘扫描与网络传输全部在工作线程执行。"""
import os
from pathlib import Path
import sys
import time

import qrcode
from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal, Qt, QLockFile, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QImage, QPainter, QPixmap, QIcon
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QInputDialog,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
    QProgressBar, QPushButton, QScrollArea, QSplitter, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget, QAbstractItemView,
)

from . import __version__, steam
from .engine import Config, Engine, WindowsSniffer, read_json
from .protocol.cloud import CloudConflict, pack_current
from .protocol.custom import write_json
from .sync import WindowsCloud
from .appinfo import cloud_support


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


class MappingDialog(QDialog):
    def __init__(self, keys, initial, parent):
        super().__init__(parent)
        self.setWindowTitle("确认云端目录与 Windows 路径的对应关系")
        self.resize(760, 400)
        self.edits = []
        layout = QVBoxLayout(self)
        layout.addWidget(label("请为云端的每个目录选择对应的 Windows 存档文件夹。相对文件名保持不变。确认后才会覆盖；两端游戏版本与存档格式也需兼容。"))
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        form = QVBoxLayout(content)
        for i, key in enumerate(keys):
            form.addWidget(label(f"目录 {i+1}：{key}"))
            row = QHBoxLayout()
            edit = QLineEdit(initial[i] if i < len(initial) else "")
            self.edits.append(edit)
            row.addWidget(edit)
            button("选择文件夹", lambda checked=False, e=edit: self.browse(e), row)
            form.addLayout(row)
        scroll.setWidget(content)
        layout.addWidget(scroll)
        controls = QDialogButtonBox()
        controls.addButton("确认目录，继续同步", QDialogButtonBox.AcceptRole).clicked.connect(self.accept)
        controls.addButton("取消", QDialogButtonBox.RejectRole).clicked.connect(self.reject)
        layout.addWidget(controls)

    def browse(self, edit):
        path = QFileDialog.getExistingDirectory(self, "选择此云端目录对应的 Windows 存档目录", edit.text())
        if path:
            edit.setText(path)

    def paths(self):
        return [e.text().strip() for e in self.edits]


class Window(QMainWindow):
    def __init__(self, data_dir):
        super().__init__()
        self.data = Path(data_dir)
        ui = read_json(self.data / "ui.json")
        self.engine = Engine(Config(str(self.data), ui.get("steam", str(steam.steam_root()))))
        self.cloud = WindowsCloud(self.engine)
        self.sniffer = WindowsSniffer(self.engine)
        self.items = []
        self.game = None
        self.busy = False
        self.remote = None
        self.auth = {}
        self.last_running = None
        self.pending_auto = []
        self.local_times = {}
        self.setWindowTitle(f"UmeYc-SteamBack-Windows  {__version__}")
        self.resize(1120, 810)
        icon = Path(__file__).parent / "assets/cloud-save.svg"
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
        self.launch_button = QPushButton("开始游戏")
        self.launch_button.clicked.connect(self.launch_game)
        page.addWidget(self.launch_button)
        page.addWidget(line())
        heading = label("云存档 · 夸克")
        heading.setStyleSheet("font-size: 17px; font-weight: 600;")
        page.addWidget(heading)
        self.times = label("本地存档时间：暂无\n云端存档时间：请刷新云端状态")
        page.addWidget(self.times)
        self.auto_download = QCheckBox("开始游戏前自动云同步（默认关闭）")
        self.auto_download.clicked.connect(self.toggle_auto_download)
        page.addWidget(self.auto_download)
        page.addWidget(label("通过本工具的“开始游戏”生效；直接从 Steam 开始无法拦截。本地有未上传进度时会暂停启动并提示。"))
        self.auto = QCheckBox("游戏关闭后自动上传云存档（默认关闭，需保持本程序运行）")
        self.auto.clicked.connect(self.toggle_auto)
        page.addWidget(self.auto)
        row = QHBoxLayout()
        button("本地同步到云端", self.upload, row)
        button("云端同步到本地", self.download, row)
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
        self.load_accounts(ui.get("account"))
        QTimer.singleShot(0, self.refresh)

    def task(self, message, function, done=None, failed=None, quiet=False):
        if self.busy:
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
            items.append({"id": selected, "name": str(selected)})
        for a in items:
            self.account_box.addItem(f"{a['name']} · {a['id']}", a["id"])
        index = self.account_box.findData(selected)
        self.account_box.setCurrentIndex(max(0, index))
        self.account_box.blockSignals(False)
        self.change_account()

    def change_account(self):
        account = self.account_box.currentData()
        self.engine.account_ids = {account} if account else set()
        self.cloud.active_account = account
        self.last_running = None
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
            support = cloud_support(self.engine.get_steam_root(), games)
            for game in games.values():
                game["steam_cloud"] = support.get(str(game["game_id"]))
            return list(games.values()), self.cloud.account_status()
        def show(result):
            self.items, self.auth = result
            self.update_auth()
            self.filter_games()
            self.status.setText(f"已加载 {len(self.items)} 个游戏。请选择 Steam 账号和游戏。")
        self.task("正在读取 Steam 游戏与夸克登录状态…", work, show)

    def update_auth(self):
        a = self.auth
        self.login_button.setText(f"{a.get('nickname') or '夸克用户'} · {a.get('user_id')}" if a.get("logged_in") else "登录夸克")
        self.login_button.setToolTip(a.get("error", "扫码登录夸克"))

    def filter_games(self):
        selected = self.game["game_id"] if self.game else None
        text = self.search.text().casefold()
        self.games_list.blockSignals(True)
        self.games_list.clear()
        groups = [(False, "不支持 Steam 云存档"), (True, "支持 Steam 云存档"), (None, "Steam 云支持待确认")]
        for support, title in groups:
            entries = [g for g in self.items if g.get("steam_cloud") is support
                       and text in f"{g['game_name']} {g['game_id']}".casefold()]
            if not entries:
                continue
            header = QListWidgetItem(f"{title}（{len(entries)}）")
            header.setFlags(Qt.NoItemFlags)
            font = header.font()
            font.setBold(True)
            header.setFont(font)
            self.games_list.addItem(header)
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
        self.backup_path.setText(f"备份保存路径：{self.engine.backup_dir(game)}")
        self.render_times()
        account = self.engine.account
        selected_game = dict(game)
        def update_local(value):
            self.local_times[(account, selected_game["game_id"])] = value
            if self.game and self.game["game_id"] == selected_game["game_id"] and self.engine.account == account:
                self.render_times()
        def read_local():
            if not self.busy and QApplication.activeModalWidget() is None:
                self.task("", lambda: self.cloud.isolated_engine(account).save_time(selected_game), update_local, quiet=True)
        QTimer.singleShot(0, read_local)
        enabled = self.cloud.read_state().get(self.cloud.key(self.engine.account, game), {}).get("auto_upload", False)
        self.auto.setChecked(enabled)
        self.auto_download.setChecked(self.cloud.read_state().get(self.cloud.key(self.engine.account, game), {}).get("auto_download", False))
        records = self.engine.backups(game)
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

    def toggle_auto(self, enabled):
        if self.require_game():
            game, account = dict(self.game), self.engine.account
            self.task("正在保存自动上传设置…", lambda: self.cloud.set_auto(account, game, enabled), lambda _: self.render_game(),
                      lambda e: (self.render_game(), self.error(e)))

    def toggle_auto_download(self, enabled):
        if self.require_game():
            game, account = dict(self.game), self.engine.account
            self.task("正在保存启动前同步设置…", lambda: self.cloud.set_auto_download(account, game, enabled),
                      lambda _: self.render_game(), lambda e: (self.render_game(), self.error(e)))

    def launch_game(self):
        if not self.require_game():
            return
        game, account = dict(self.game), self.engine.account
        def start(result):
            if self.engine.account != account:
                return self.error(ValueError("Steam 账号已变化，已取消启动"))
            self.engine.guard(game)
            if not QDesktopServices.openUrl(QUrl(f"steam://rungameid/{int(game['game_id'])}")):
                raise ValueError("无法唤起 Steam，请确认已安装并登录 Steam")
            self.status.setText(result["message"] + "；已请求 Steam 启动游戏")
            self.render_game()
        self.task("正在检查启动前云存档同步，请稍候…", lambda: self.cloud.prepare_game_launch(account, game), start)

    def upload(self):
        if not self.require_game():
            return
        game, account = dict(self.game), self.engine.account
        def conflict(error):
            if isinstance(error, CloudConflict):
                if self.confirm(f"云端存在未同步版本（存档时间：{date(error.remote.get('save_time'))}）。用当前本地存档替换？云端旧版本会删除。"):
                    self.task("正在确认版本并上传…", lambda: self.cloud.upload(account, game, error.remote.get("revision")), self.synced, conflict)
            else:
                self.error(error)
        self.task("正在打包并上传到夸克…", lambda: self.cloud.upload(account, game), self.synced, conflict)

    def synced(self, result):
        self.status.setText(result["message"])
        self.remote = None
        self.render_game()

    def download(self):
        if not self.require_game():
            return
        game, account = dict(self.game), self.engine.account
        self.task("正在校验云端存档并读取目录映射…", lambda: self.cloud.inspect(account, game),
                  lambda inspected: self.confirm_download(game, account, inspected))

    def confirm_download(self, game, account, inspected):
        manifest, remote = inspected["manifest"], inspected["remote"]
        settings = self.engine.settings(game)
        dialog = MappingDialog(manifest["root_keys"], settings.get("paths", []), self)
        if dialog.exec() != QDialog.Accepted:
            return
        if not self.confirm(f"云端存档时间：{date(remote['save_time'])}\n将覆盖所选 Windows 目录中的对应文件；覆盖前保存本地备份。确定继续？"):
            return
        self.engine.configure(game, dialog.paths(), manifest["root_keys"], manifest["mode"])
        self.task("正在同步到本地并保存覆盖前备份…", lambda: self.cloud.download(account, game, remote["revision"], inspected["uid"]), self.synced)

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
        if self.busy or not self.engine.account_ids or QApplication.activeModalWidget() is not None:
            return
        account = self.engine.account
        items = list(self.items)
        def observed(running):
            self.cloud.running = {(account, aid) for aid in running}
            if self.last_running is not None:
                exited = self.last_running - running
                for game in items:
                    if game["game_id"] in exited and self.cloud.read_state().get(self.cloud.key(account, game), {}).get("auto_upload"):
                        self.pending_auto.append((time.monotonic() + 6, game))
            self.last_running = running
            ready = next(((when, g) for when, g in self.pending_auto if time.monotonic() >= when), None)
            if ready:
                self.pending_auto.remove(ready)
                game = ready[1]
                if game["game_id"] not in running:
                    self.task(f"{game['game_name']} 已退出，正在自动上传…",
                              lambda: self.cloud.upload(account, game), self.synced,
                              lambda e: self.status.setText(f"自动上传已停止：{e}。请检查后手动同步。"))
        self.task("", lambda: steam.running_games(items), observed, quiet=True)

    def closeEvent(self, event):
        if self.busy:
            self.status.setText("正在处理存档，请等待当前操作完成后退出")
            event.ignore()
        else:
            event.accept()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("UmeYc-SteamBack-Windows")
    app.setOrganizationName("xiaochaom")
    smoke_temp = None
    if "--smoke-test" in sys.argv:
        import tempfile
        smoke_temp = tempfile.TemporaryDirectory()
        data = Path(smoke_temp.name) / "data"
        write_json(data / "ui.json", {"steam": str(Path(smoke_temp.name) / "Steam")})
    elif os.name == "nt":
        data = Path(os.environ["LOCALAPPDATA"]) / "UmeYc-SteamBack-Windows"
    else:
        data = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "UmeYc-SteamBack-Windows"
    data.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(data / ".application.lock"))
    if not lock.tryLock(100):
        QMessageBox.information(None, "UmeYc-SteamBack-Windows", "程序已在运行，请使用已打开的窗口")
        return
    app.setStyle("Fusion")
    window = Window(data)
    window.show()
    if smoke_temp:
        window.monitor.stop()
        QTimer.singleShot(2000, app.quit)
    app.exec()
    lock.unlock()
    if smoke_temp:
        smoke_temp.cleanup()
