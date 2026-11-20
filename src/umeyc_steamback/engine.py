"""本地路径映射、不可滚动删除的备份和恢复保护。"""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import uuid

from . import steam
from .protocol.cloud import pack_current, unpack_verified, digest
from .protocol.custom import safe_file, write_json
from .protocol.sniff import SaveSniffer


@dataclass
class Config:
    app_data_dir: str
    steam_dir: str
    prelaunch_game_id: int | None = None


def read_json(path, default=None):
    path = Path(path)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else ({} if default is None else default)


def linked(path):
    try:
        return path.is_symlink() or bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400)
    except FileNotFoundError:
        return False


class Engine:
    def __init__(self, config):
        self.config = config
        self.account_ids = set()
        self.data = Path(config.app_data_dir)
        self.data.mkdir(parents=True, exist_ok=True)

    @property
    def account(self):
        if len(self.account_ids) != 1:
            raise ValueError("请先选择 Steam 账号")
        return next(iter(self.account_ids))

    def get_steam_root(self):
        return self.config.steam_dir

    def _parse_installdir(self, game):
        return game.get("install_dir", "")

    def key(self, game):
        return f"{self.account}:{int(game['game_id'])}"

    def settings(self, game):
        return read_json(self.data / "games.json").get(self.key(game), {})

    def configured_games(self):
        return [v["game"] for k, v in read_json(self.data / "games.json").items()
                if k.startswith(f"{self.account}:")]

    def configure(self, game, paths, keys=None, mode="custom"):
        if mode not in ("custom", "auto"):
            raise ValueError("无效的云端路径模式")
        normalized = []
        for text in paths:
            path = Path(text).expanduser()
            if not path.is_absolute() or not path.exists() or linked(path):
                raise ValueError(f"请选择存在的实际存档文件或目录：{path}")
            if not path.is_file() and not path.is_dir():
                raise ValueError("请选择普通文件或目录")
            path = path.resolve()
            if path == Path(path.anchor) or path == Path.home().resolve():
                raise ValueError("请选择具体游戏的存档目录")
            data = self.data.resolve()
            if path == data or path in data.parents or data in path.parents:
                raise ValueError("存档目录与应用备份目录不能相互包含")
            normalized.append(str(path))
        if not normalized:
            raise ValueError("请至少选择一个存档文件或目录")
        if len(set(normalized)) != len(normalized):
            raise ValueError("不能重复选择同一目录")
        if any(Path(a) in Path(b).parents for a in normalized for b in normalized if a != b):
            raise ValueError("所选存档目录不能相互包含")
        # Folder mappings preserve the remote slot order exactly.
        if keys is not None:
            if len(keys) != len(normalized) or not keys or len(keys) > 100 or not all(isinstance(k, str) for k in keys):
                raise ValueError("云端目录映射不完整")
            if not all(Path(p).is_dir() for p in normalized):
                raise ValueError("云端目录映射需要选择文件夹")
        state = read_json(self.data / "games.json")
        state[self.key(game)] = {"game": game, "paths": normalized, "keys": keys, "mode": mode}
        write_json(self.data / "games.json", state)

    def _custom_plan(self, game):
        settings = self.settings(game)
        if not settings.get("paths"):
            raise ValueError("请先选择存档路径")
        grouped = {}
        for value in settings["paths"]:
            path = Path(value)
            if not path.exists() or linked(path):
                raise ValueError(f"存档路径不可用：{path}")
            root = path.parent if path.is_file() else path
            files = grouped.setdefault(str(root), set())
            if path.is_file():
                safe_file(root, path.name)
                files.add(path.name)
            else:
                for directory, dirs, names in os.walk(root, followlinks=False):
                    dirs[:] = sorted(d for d in dirs if not linked(Path(directory) / d))
                    for name in sorted(names):
                        p = Path(directory) / name
                        if linked(p) or not p.is_file():
                            continue
                        relative = p.relative_to(root).as_posix()
                        safe_file(root, relative)
                        files.add(relative)
        roots, file_map = {}, {}
        for i, (root, files) in enumerate(grouped.items()):
            suffix = "" if i == 0 else f"_{i}"
            roots[root] = suffix
            file_map[suffix] = sorted(files)
        keys = settings.get("keys") or [f"custom-{i}" for i in range(len(roots))]
        if len(keys) != len(roots):
            raise ValueError("目录映射已变化，请重新配置")
        return {**game, "save_games_roots": roots, "custom_files": file_map,
                "portable_keys": keys, "portable_mode": settings.get("mode", "custom")}

    def save_time(self, game):
        try:
            plan = self._custom_plan(game)
            values = [safe_file(root, f).stat().st_mtime_ns // 1000000
                      for root, suffix in plan["save_games_roots"].items() for f in plan["custom_files"][suffix]]
            return max(values, default=None)
        except (ValueError, OSError):
            return None

    def guard(self, game):
        if self.config.prelaunch_game_id is not None and not game.get("install_dir"):
            manifest = Path(game["install_root"]) / "steamapps" / f"appmanifest_{game['game_id']}.acf"
            install = steam.read_vdf(manifest).get("AppState", {}).get("installdir")
            if not install:
                raise ValueError("无法确认游戏安装目录，请刷新 Steam 游戏列表后重试")
            game = {**game, "install_dir": install}
        if game["game_id"] in steam.running_games([game], **(
                {"ignore_registry_for": self.config.prelaunch_game_id} if self.config.prelaunch_game_id is not None else {})):
            raise ValueError("游戏正在运行，请退出游戏后操作存档")

    def backup_dir(self, game):
        return self.data / "backups" / str(self.account) / str(int(game["game_id"]))

    def backup(self, game, source="manual", allow_empty=False):
        self.guard(game)
        folder = self.backup_dir(game)
        folder.mkdir(parents=True, exist_ok=True)
        name = f"{int(time.time()*1000)}-{uuid.uuid4().hex}"
        archive = folder / f"{name}.zip"
        try:
            plan = self._custom_plan(game)
            if allow_empty and not any(plan["custom_files"].values()):
                record = {"id": name, "name": "", "source": source, "time": int(time.time()*1000),
                          "save_time": None, "empty": True, "mode": plan["portable_mode"],
                          "root_keys": plan["portable_keys"]}
                write_json(folder / f"{name}.json", record)
                return record
            manifest = pack_current(self, game, archive)
            record = {"id": name, "name": "", "source": source, "time": int(time.time()*1000),
                      "save_time": manifest["save_time"], "content_hash": manifest["content_hash"],
                      "archive_hash": digest(archive)}
            write_json(folder / f"{name}.json", record)
            return record
        except BaseException:
            archive.unlink(missing_ok=True)
            raise

    def backups(self, game):
        return sorted([read_json(p) for p in self.backup_dir(game).glob("*.json")],
                      key=lambda b: b["time"], reverse=True)

    def record_paths(self, game, record_id):
        if not isinstance(record_id, str) or not __import__("re").fullmatch(r"\d+-[a-f0-9]{32}", record_id):
            raise ValueError("无效的备份编号")
        folder = self.backup_dir(game)
        return folder / f"{record_id}.zip", folder / f"{record_id}.json"

    def rename(self, game, record_id, name):
        _, meta = self.record_paths(game, record_id)
        record = read_json(meta)
        record["name"] = str(name).strip()[:100]
        write_json(meta, record)

    def delete(self, game, record_id):
        archive, meta = self.record_paths(game, record_id)
        meta.unlink()
        archive.unlink(missing_ok=True)

    def apply(self, game, manifest, staging, source, remove=()):
        self.guard(game)
        plan = self._custom_plan(game)
        if manifest["root_keys"] != plan["portable_keys"] or manifest["mode"] != plan["portable_mode"]:
            raise ValueError("存档的目录映射与本机不同，请先配置对应目录")
        roots = list(plan["save_games_roots"])
        configured = [Path(p) for p in self.settings(game)["paths"]]
        targets = []
        seen = set()
        for f in manifest["files"]:
            relative = f["path"]
            target = safe_file(roots[f["slot"]], relative)
            identity = str(target).casefold()
            if identity in seen:
                raise ValueError("存档文件名在 Windows 下存在大小写冲突")
            seen.add(identity)
            if not any(target == p or (p.is_dir() and p in target.parents) for p in configured):
                raise ValueError("存档超出已选择的文件范围，请改为选择对应文件夹")
            if target.exists() and not target.is_file():
                raise ValueError("目标文件与现有目录冲突")
            targets.append((target, Path(staging) / str(f["slot"]) / relative, f))
        for f in remove:
            if type(f.get("slot")) is not int or not 0 <= f["slot"] < len(roots):
                raise ValueError("覆盖前备份的目录映射无效")
            target = safe_file(roots[f["slot"]], f["path"])
            if not any(target == p or (p.is_dir() and p in target.parents) for p in configured):
                raise ValueError("恢复记录超出已选择的文件范围")
            if str(target).casefold() in seen:
                raise ValueError("恢复记录包含重复文件")
            seen.add(str(target).casefold())
            if target.exists() and not target.is_file():
                raise ValueError("恢复记录与现有目录冲突")
            targets.append((target, None, f))
        # Persistent safety snapshot survives app restarts. No rolling deletion.
        before = self.backup(game, source=f"before_{source}", allow_empty=True)
        self.guard(game)
        if before:
            _, meta = self.record_paths(game, before["id"])
            before["remove_on_restore"] = [{"slot": f["slot"], "path": f["path"]}
                                           for t, src, f in targets if src is not None and not t.exists()]
            write_json(meta, before)
        # Roll back this operation if any replacement fails. Staging stays off the live path.
        with tempfile.TemporaryDirectory(dir=self.data) as temp:
            rollback = []
            try:
                for i, (target, src, f) in enumerate(targets):
                    target = safe_file(roots[f["slot"]], f["path"])
                    old = Path(temp) / str(i)
                    exists = target.exists()
                    if exists:
                        shutil.copy2(target, old)
                    rollback.append((target, old if exists else None))
                    if src is None:
                        target.unlink(missing_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    fd, pending = tempfile.mkstemp(dir=target.parent, prefix=".umeyc-")
                    os.close(fd)
                    try:
                        shutil.copy2(src, pending)
                        os.replace(pending, target)
                    finally:
                        Path(pending).unlink(missing_ok=True)
            except BaseException:
                for target, old in reversed(rollback):
                    if old:
                        shutil.copy2(old, target)
                    else:
                        target.unlink(missing_ok=True)
                raise
        return before

    def restore(self, game, record_id):
        archive, meta = self.record_paths(game, record_id)
        record = read_json(meta)
        if record.get("empty"):
            manifest = {"mode": record["mode"], "root_keys": record["root_keys"], "files": []}
            self.apply(game, manifest, None, "restore", record.get("remove_on_restore", []))
            return
        if digest(archive) != record["archive_hash"]:
            raise ValueError("本地备份校验失败，未覆盖游戏存档")
        with tempfile.TemporaryDirectory(dir=self.data) as temp:
            manifest = unpack_verified(archive, temp, game, self.account, record["content_hash"])
            self.apply(game, manifest, temp, "restore", record.get("remove_on_restore", []))


class WindowsSniffer(SaveSniffer):
    def roots(self, game, account):
        return list(dict.fromkeys([Path(self.engine.get_steam_root()) / "userdata" / str(account) / str(game["game_id"]),
                                  steam.game_directory(game), *steam.user_folders().values()]))
