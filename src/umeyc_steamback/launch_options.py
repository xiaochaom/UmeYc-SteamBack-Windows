"""离线修改 Steam 单游戏启动选项，原值备份并支持精确恢复。"""
import hashlib
import io
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import uuid

import psutil
import vdf

from .desktop import app_command
from .engine import read_json
from .protocol.custom import safe_file, write_json


def steam_is_running():
    for process in psutil.process_iter(["name"]):
        try:
            if (process.info["name"] or "").lower() in {"steam.exe", "steam"}:
                return True
        except psutil.Error:
            continue
    return False


def child(mapping, name, create=False):
    found = [key for key in mapping if str(key).casefold() == name.casefold()]
    if len(found) > 1:
        raise ValueError("Steam 配置存在重复字段，未修改")
    if not found:
        if not create:
            return None
        mapping[name] = vdf.VDFDict()
        return mapping[name]
    value = mapping[found[0]]
    if not isinstance(value, dict):
        raise ValueError("Steam 配置结构异常，未修改")
    return value


class LaunchOptions:
    def __init__(self, engine):
        self.engine = engine
        self.state_path = engine.data / "steam_launch_options.json"

    def path(self, account):
        if type(account) is not int or not 0 < account < 2**32:
            raise ValueError("Steam 账号无效")
        return Path(self.engine.get_steam_root()) / "userdata" / str(account) / "config/localconfig.vdf"

    def read(self, account, game_id):
        path = self.path(account)
        raw = path.read_bytes()
        if len(raw) > 64 * 1024**2:
            raise ValueError("Steam 配置过大，未修改")
        try:
            tree = vdf.load(io.StringIO(raw.decode("utf-8-sig")), mapper=vdf.VDFDict, merge_duplicate_keys=False)
            node = tree
            for key in ("UserLocalConfigStore", "Software", "Valve", "Steam", "apps", str(int(game_id))):
                node = child(node, key, create=True)
            keys = [k for k in node if k.casefold() == "launchoptions"]
            if len(keys) > 1:
                raise ValueError("Steam 启动选项重复，未修改")
            field = keys[0] if keys else "LaunchOptions"
            value = node.get(field, "")
            if not isinstance(value, str) or "\x00" in value:
                raise ValueError("Steam 启动选项格式异常")
            return path, raw, tree, node, field, value, bool(keys)
        except (UnicodeError, SyntaxError, TypeError, AttributeError) as error:
            raise ValueError("无法安全读取 Steam 配置，未修改") from error

    @staticmethod
    def prefix(account, game_id):
        # Force quoting of the EXE even if installed in a path without spaces.
        args = app_command("--steam-launch", "--account", str(account), "--game-id", str(game_id), "--")
        exe = args[0]
        if any(c in exe for c in ('"', '%', '\n', '\r')):
            raise ValueError("程序安装路径包含 Steam 不支持的字符，请移动程序目录")
        return '"' + exe + '" ' + subprocess.list2cmdline(args[1:]) + " "

    def preview(self, account, game_id):
        key = f"{account}:{int(game_id)}"
        state = read_json(self.state_path).get(key)
        *_, current, existed = self.read(account, game_id)
        if state and current == state["installed"]:
            current = state["original"]
        elif state and current != state["original"]:
            raise ValueError("Steam 启动选项已被修改，请先检查并恢复原启动项")
        if "--steam-launch" in current:
            raise ValueError("已存在同步启动项，无法安全叠加")
        wrapped = current if re.search(r"%command%", current, re.I) else "%command%" + (" " + current if current else "")
        installed = self.prefix(account, game_id) + wrapped
        if len(installed) > 7000:
            raise ValueError("启动选项过长")
        return current, installed

    def remember_preview(self, account, game_id, installed):
        *_, original, existed = self.read(account, game_id)
        key = f"{account}:{int(game_id)}"
        state = read_json(self.state_path)
        previous = state.get(key)
        if previous and original == previous["installed"]:
            # Reconfiguring after moving the tool updates only our prefix,
            # retaining the parameters from before the first installation.
            previous["installed"] = installed
            write_json(self.state_path, state)
            return
        state[key] = {"original": original, "installed": installed, "existed": existed}
        write_json(self.state_path, state)

    def installed(self, account, game_id):
        entry = self.configured(account, game_id)
        if not entry:
            return False
        try:
            return self.read(account, game_id)[5] == entry["installed"]
        except (OSError, ValueError):
            return False

    def configured(self, account, game_id):
        return read_json(self.state_path).get(f"{account}:{int(game_id)}")

    def update(self, account, game_id, restore=False):
        if steam_is_running():
            raise ValueError("请先完全退出 Steam，再配置或恢复启动项；完成后可正常打开 Steam")
        key = f"{account}:{int(game_id)}"
        state = read_json(self.state_path)
        path, raw, tree, node, field, current, existed = self.read(account, game_id)
        old = state.get(key)
        if restore:
            if not old:
                raise ValueError("没有本工具保存的原启动项")
            if current != old["installed"]:
                raise ValueError("Steam 启动项已被其他程序或用户修改，已停止恢复以保护新设置")
            if old["existed"]:
                del node[field]
                node[field] = old["original"]
            else:
                del node[field]
            del state[key]
        else:
            if old and current == old["installed"]:
                return
            original, installed = self.preview(account, game_id)
            if existed:
                del node[field]
            node[field] = installed
            state[key] = {"original": original, "installed": installed, "existed": existed}
        output = vdf.dumps(tree, pretty=True).encode("utf-8")
        # Verify serialized data can be read before touching the real config.
        vdf.loads(output.decode(), mapper=vdf.VDFDict, merge_duplicate_keys=False)
        folder = self.engine.data / "steam_config_backups"
        folder.mkdir(parents=True, exist_ok=True)
        backup = folder / f"{account}-{int(time.time()*1000)}-{uuid.uuid4().hex}.vdf"
        backup.write_bytes(raw)
        # Persist recovery metadata before modifying Steam, including original bytes.
        intent = self.engine.data / "steam_launch_recovery.json"
        write_json(intent, {"path": str(path), "backup": str(backup.relative_to(self.engine.data)), "before": read_json(self.state_path), "after": state,
                            "output_sha256": hashlib.sha256(output).hexdigest()})
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".umeyc-")
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(output)
            if steam_is_running() or path.read_bytes() != raw:
                raise ValueError("Steam 已启动或配置发生变化，已取消修改，请退出 Steam 后重试")
            os.replace(temporary, path)
            write_json(self.state_path, state)
            intent.unlink()
        finally:
            Path(temporary).unlink(missing_ok=True)

    def recover(self):
        intent = self.engine.data / "steam_launch_recovery.json"
        if not intent.exists():
            return
        saved = read_json(intent)
        current = Path(saved["path"]).read_bytes()
        if hashlib.sha256(current).hexdigest() == saved["output_sha256"]:
            write_json(self.state_path, saved["after"])
        elif current == safe_file(self.engine.data / "steam_config_backups",
                                  saved["backup"].replace("\\", "/").rsplit("/", 1)[-1]).read_bytes():
            write_json(self.state_path, saved["before"])
        else:
            raise ValueError("存在未完成的 Steam 启动项修改，且配置已变化，请检查备份目录")
        intent.unlink()
