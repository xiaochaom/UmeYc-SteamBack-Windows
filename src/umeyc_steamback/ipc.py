"""同用户单实例通信；仅接收显示窗口、同步请求，不接收执行命令。"""
import hashlib
import json
import math
import re

from PySide6.QtCore import QObject, Signal, QTimer
from PySide6.QtNetwork import QLocalServer, QLocalSocket


def valid_launch_command(value):
    if not isinstance(value, dict) or value.get("action") not in {"prepare", "finished"}:
        return False
    base = {"action", "account", "game_id"}
    owner = {"session", "pid", "started"}
    if set(value) not in (base, base | owner):
        return False
    if not all(type(value[k]) is int and 0 < value[k] < 2**32 for k in ("account", "game_id")):
        return False
    if "session" not in value:
        return True  # Existing launchers can finish across an app upgrade.
    return (isinstance(value["session"], str) and re.fullmatch(r"[a-f0-9]{32}", value["session"]) is not None
            and type(value["pid"]) is int and 0 < value["pid"] < 2**32
            and type(value["started"]) in (float, int) and math.isfinite(value["started"]) and value["started"] > 0)


def server_name(data):
    return "UmeYc-SteamBack-" + hashlib.sha256(str(data.resolve()).encode()).hexdigest()[:24]


def request(name, command, timeout=2000):
    socket = QLocalSocket()
    socket.connectToServer(name)
    if not socket.waitForConnected(1000):
        return None
    socket.write(json.dumps(command).encode() + b"\n")
    socket.flush()
    if not socket.waitForReadyRead(timeout):
        socket.abort()
        raise ValueError("后台同步等待超时，游戏未启动")
    data = bytearray()
    while not data.endswith(b"\n") and len(data) <= 4096:
        data.extend(bytes(socket.readAll()))
        if not data.endswith(b"\n") and not socket.waitForReadyRead(2000):
            raise ValueError("后台同步响应不完整")
    socket.disconnectFromServer()
    return json.loads(data)


class CommandServer(QObject):
    command = Signal(object, object)

    def __init__(self, name, parent=None):
        super().__init__(parent)
        self.server = QLocalServer(self)
        self.server.setSocketOptions(QLocalServer.UserAccessOption)
        self.sockets = set()
        self.accepted = set()
        self.server.newConnection.connect(self.accept)
        QLocalServer.removeServer(name)  # Caller must own the application lock.
        if not self.server.listen(name):
            raise OSError("无法创建本机启动入口：" + self.server.errorString())

    def accept(self):
        while self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            self.sockets.add(socket)
            socket.readyRead.connect(lambda s=socket: self.read(s))
            socket.disconnected.connect(lambda s=socket: self.release(s))
            QTimer.singleShot(5000, lambda s=socket: s.disconnectFromServer() if s in self.sockets and s not in self.accepted else None)
            self.read(socket)

    def release(self, socket):
        self.sockets.discard(socket)
        self.accepted.discard(socket)
        socket.deleteLater()

    def read(self, socket):
        if socket in self.accepted:
            return
        if socket.bytesAvailable() > 4096:
            socket.disconnectFromServer()
            return
        if not socket.canReadLine():
            return
        try:
            value = json.loads(bytes(socket.readLine()))
            if value == {"action": "show"}:
                pass
            elif valid_launch_command(value):
                pass
            else:
                raise ValueError("无效启动命令")
        except (ValueError, TypeError):
            socket.disconnectFromServer()
            return
        self.accepted.add(socket)
        def reply(result):
            if socket not in self.sockets:
                return
            socket.write(json.dumps(result, ensure_ascii=False).encode() + b"\n")
            socket.flush()
            socket.disconnectFromServer()
        self.command.emit(value, reply)
