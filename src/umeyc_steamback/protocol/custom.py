"""本地存档配置与内容校验；无需 Steam 云存档支持。"""

import hashlib
import json
import os
from pathlib import Path
from pathlib import PureWindowsPath, PurePosixPath
import re
import tempfile


def write_json(path, value):
    """原子替换元数据，避免中途退出损坏配置和固定标记。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".steamback-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def safe_file(root, relative):
    """清单只允许根目录内的普通文件，拒绝路径穿越和符号链接。"""
    text = str(relative)
    posix = PurePosixPath(text)
    win = PureWindowsPath(text)
    reserved = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    if (win.drive or win.root or "\\" in text or not posix.parts or str(posix) != text
            or posix.is_absolute() or ".." in posix.parts
            or any(re.search(r'[<>:"|?*\x00-\x1f]', p) or p.endswith((" ", "."))
                   or p.split(".")[0].upper() in reserved
                   or re.fullmatch(r"(?:COM|LPT)[1-9¹²³]", p.split(".")[0], re.I)
                   for p in posix.parts)):
        raise ValueError("存档清单包含无效路径")
    relative = Path(*posix.parts)
    root = Path(root).absolute()
    for part in (root, *root.parents):
        if part.is_symlink() or (part.exists() and getattr(part.lstat(), "st_file_attributes", 0) & 0x400):
            raise ValueError("存档路径包含符号链接或目录联接，请选择实际路径")
    root = root.resolve()
    target = root / relative
    current = target
    while current != root:
        if current.is_symlink() or (current.exists() and getattr(current.lstat(), "st_file_attributes", 0) & 0x400):
            raise ValueError("存档路径包含符号链接，请选择实际路径")
        current = current.parent
    return target


class CustomSaves:
    def get_custom_games(self):
        path = Path(self.config.app_data_dir) / "custom_games.json"
        if not path.exists():
            return []
        with path.open(encoding="utf-8") as stream:
            return list(json.load(stream).values())

    def set_custom_game(self, game_info, paths):
        info = {
            "game_id": int(game_info["game_id"]),
            "game_name": str(game_info["game_name"]).strip(),
            "install_root": game_info.get("install_root", self.get_steam_root()),
        }
        if info["game_id"] <= 0 or not info["game_name"]:
            raise ValueError("请填写有效的游戏 ID 和名称")
        normalized = []
        data = Path(self.config.app_data_dir).resolve()
        for value in paths:
            raw = Path(value.strip()).expanduser()
            if not raw.is_absolute() or not raw.exists():
                raise ValueError(f"存档路径不存在或不是绝对路径：{value}")
            path = raw.resolve()
            if path == Path(path.anchor) or path == Path.home().resolve():
                raise ValueError("请选择具体的存档文件或目录")
            if path == data or path in data.parents or data in path.parents:
                raise ValueError("存档路径不能包含或位于备份数据目录")
            if not path.is_file() and not path.is_dir():
                raise ValueError("只能备份普通文件或目录")
            if str(path) not in normalized:
                normalized.append(str(path))
        for value in normalized:
            if any(Path(other) in Path(value).parents for other in normalized if other != value):
                raise ValueError("多个存档路径不能相互包含")
        games = {str(g["game_id"]): g for g in self.get_custom_games()}
        if normalized:
            info["paths"] = normalized
            games[str(info["game_id"])] = info
        else:
            games.pop(str(info["game_id"]), None)
        write_json(Path(self.config.app_data_dir) / "custom_games.json", games)
        return info

    def _custom_plan(self, game_info):
        configured = next((g for g in self.get_custom_games()
                           if g["game_id"] == game_info["game_id"]), None)
        if configured is None:
            return None
        info = dict(game_info)
        info["save_games_roots"] = {}
        info["custom_files"] = {}
        grouped = {}
        for value in configured["paths"]:
            path = Path(value)
            if not path.exists():
                raise ValueError(f"存档路径暂不可用，请检查磁盘挂载：{path}")
            if path.resolve() != path:
                raise ValueError(f"存档路径已变为符号链接，请重新选择：{path}")
            root = path.parent if path.is_file() else path
            files = [path.name] if path.is_file() else sorted(
                str(p.relative_to(root)) for p in path.rglob("*")
                if p.is_file() and not p.is_symlink()
                and not any(parent.is_symlink() for parent in p.parents if parent != root))
            grouped.setdefault(str(root), set()).update(files)
        for index, (root, files) in enumerate(grouped.items()):
            suffix = "" if index == 0 else f"_{index}"
            info["save_games_roots"][root] = suffix
            info["custom_files"][suffix] = sorted(files)
        return info

    def _fingerprint(self, game_info, backup_basename=None):
        digest = hashlib.sha256()
        for root, suffix in game_info["save_games_roots"].items():
            digest.update(json.dumps([root, game_info["custom_files"][suffix]],
                                     ensure_ascii=False).encode())
            for relative in game_info["custom_files"][suffix]:
                file_digest = hashlib.sha256()
                source = backup_basename + suffix if backup_basename is not None else root
                with safe_file(source, relative).open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        file_digest.update(chunk)
                digest.update(file_digest.digest())
        return digest.hexdigest()
