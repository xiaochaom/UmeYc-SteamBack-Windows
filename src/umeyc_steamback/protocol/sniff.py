"""用户主动开启的文件变更对比；只返回候选，不自动采用或上传。"""
import os
from pathlib import Path
import threading
import time


class SaveSniffer:
    MAX_FILES = 100000
    MAX_SECONDS = 20
    SKIP = {"cache", "caches", "logs", "log", "shadercache", "gpucache", "webcache", "temp", "tmp",
            "node_modules", ".git", "browsercache", "crashdumps", "crashreportclient", "redist", "__pycache__"}
    IGNORE_EXT = {".log", ".tmp", ".exe", ".dll", ".so", ".pak", ".pdb", ".vpk", ".mp4", ".webm"}

    def __init__(self, engine, home=None):
        self.engine = engine
        self.home = Path(home) if home is not None else Path.home()
        self.sessions = {}
        self.lock = threading.RLock()

    def roots(self, game, account):
        steam = Path(self.engine.get_steam_root())
        install = Path(game["install_root"])
        roots = [steam / "userdata" / str(account) / str(game["game_id"])]
        for library in dict.fromkeys([steam, install]):
            roots.append(library / "steamapps/compatdata" / str(game["game_id"]) / "pfx/drive_c/users/steamuser")
        directory = self.engine._parse_installdir(game)
        if directory:
            roots.append(install / "steamapps/common" / directory)
        roots += [self.home / name for name in (".local/share", ".config", "Documents", "Saved Games")]
        return list(dict.fromkeys(p.resolve() for p in roots))

    def scan(self, roots):
        result, truncated = {}, False
        deadline = time.monotonic() + self.MAX_SECONDS
        data = Path(self.engine.config.app_data_dir).resolve()
        steam = Path(self.engine.get_steam_root()).resolve()
        # Scan Steam only through explicit game-specific roots, never via home.
        excluded = [data, steam, (self.home / "homebrew").resolve()]
        def linked(path):
            try:
                return path.is_symlink() or bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400)
            except OSError:
                return True
        for root in roots:
            if linked(root) or not root.is_dir():
                continue
            for directory, dirs, files in os.walk(root, followlinks=False):
                if time.monotonic() >= deadline:
                    return result, True
                parent = Path(directory)
                dirs[:] = sorted(d for d in dirs if d.lower() not in self.SKIP and not linked(parent / d)
                                 and not any((parent / d).resolve() == x for x in excluded))
                for name in sorted(files):
                    path = parent / name
                    if path.suffix.lower() in self.IGNORE_EXT or name == "steam_autocloud.vdf":
                        continue
                    if linked(path) or not path.is_file():
                        continue
                    try:
                        s = path.stat()
                    except OSError:
                        continue
                    result[str(path)] = (s.st_mtime_ns, s.st_size)
                    if len(result) >= self.MAX_FILES or time.monotonic() >= deadline:
                        truncated = True
                        return result, truncated
        return result, truncated

    def begin(self, game, account):
        with self.lock:
            key = (account, int(game["game_id"]))
            if len(self.sessions) >= 5 and key not in self.sessions:
                raise ValueError("同时查找的游戏过多，请先结束其他查找")
            roots = self.roots(game, account)
            before, truncated = self.scan(roots)
            self.sessions[key] = {"game": dict(game), "roots": roots, "before": before,
                                  "truncated": truncated, "started": time.monotonic(), "state": "watching"}
            return {"state": "watching", "truncated": truncated,
                    "message": "请启动游戏并保存一次进度，退出游戏后点击完成查找"}

    def finish(self, game, account):
        with self.lock:
            session = self.sessions.get((account, int(game["game_id"])))
            if not session:
                raise ValueError("请先开始查找，再进入游戏保存进度")
            if time.monotonic() - session["started"] > 24 * 3600:
                raise ValueError("查找已超过一天，请重新开始")
            after, truncated = self.scan(session["roots"])
            candidates = []
            for name, (modified, size) in after.items():
                if session["before"].get(name) == (modified, size) or not size:
                    continue
                p = Path(name)
                lower = name.lower()
                score = 0
                if p.suffix.lower() in {".sav", ".save", ".dat", ".sl2", ".ess", ".rpgsave", ".srm"}:
                    score += 5
                if any(s in lower for s in ("saved", "savegame", "saves", "savedata", "profile")):
                    score += 3
                if "compatdata/" + str(game["game_id"]) + "/" in name:
                    score += 2
                candidates.append({"path": name, "modified": modified // 1000000, "size": size, "score": score})
            candidates.sort(key=lambda x: (-x["score"], -x["modified"], x["path"]))
            session.update(state="ready", candidates=candidates[:200])
            return {"state": "ready", "candidates": candidates[:200],
                    "truncated": truncated or session["truncated"] or len(candidates) > 200,
                    "message": "这些是变更文件，可能包含配置或缓存。请选择实际存档；不会自动采用。"}

    def status(self, game, account):
        with self.lock:
            s = self.sessions.get((account, int(game["game_id"])))
            return {"state": s["state"], "candidates": s.get("candidates", []), "truncated": s["truncated"]} if s else {"state": "idle"}

    def cancel(self, game, account):
        with self.lock:
            self.sessions.pop((account, int(game["game_id"])), None)
            return {"state": "idle"}
