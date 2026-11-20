"""Steam 库、账号与游戏进程发现，不需要游戏支持 Steam 云。"""
import os
from pathlib import Path
import re

import psutil
import vdf

STEAM_ID_BASE = 76561197960265728


def is_compatibility_tool(name):
    return bool(re.fullmatch(
        r"(?:proton(?:\s*[-_]\s*|\s+)?(?:\d[\w.\s-]*|experimental|hotfix|next|bleeding[ -]edge)"
        r"|(?:ge[-_ ]?proton|proton[-_ ]?ge)(?:[-_ ]?\d[\w.\s-]*)?"
        r"|steam linux runtime(?:\s*[-_]\s*|\s+)?(?:\d[\w.\s-]*(?:\([a-z]+\))?|soldier|sniper|scout)?|proton)",
        name.strip(), re.I))


def read_vdf(path):
    try:
        with Path(path).open(encoding="utf-8-sig", errors="replace") as stream:
            return vdf.load(stream)
    except (OSError, ValueError, SyntaxError):
        return {}


def steam_root():
    if os.name == "nt":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
                return Path(winreg.QueryValueEx(key, "SteamPath")[0])
        except OSError:
            pass
    return Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)")) / "Steam"


def accounts(root):
    result = []
    for sid, entry in read_vdf(root / "config/loginusers.vdf").get("users", {}).items():
        if sid.isdigit() and 0 < int(sid) - STEAM_ID_BASE < 2**32:
            result.append({"id": int(sid) - STEAM_ID_BASE,
                           "name": entry.get("PersonaName") or "Steam 用户",
                           "recent": entry.get("MostRecent") == "1"})
    for path in sorted((root / "userdata").glob("*")):
        if path.is_dir() and path.name.isdigit() and 0 < int(path.name) < 2**32:
            if not any(a["id"] == int(path.name) for a in result):
                result.append({"id": int(path.name), "name": "Steam 用户", "recent": False})
    return sorted(result, key=lambda a: not a["recent"])


def games(root):
    libraries = [root]
    for entry in read_vdf(root / "steamapps/libraryfolders.vdf").get("libraryfolders", {}).values():
        if isinstance(entry, dict) and entry.get("path"):
            libraries.append(Path(entry["path"]))
    result = {}
    for library in dict.fromkeys(libraries):
        for manifest in sorted((library / "steamapps").glob("appmanifest_*.acf")):
            app = read_vdf(manifest).get("AppState", {})
            if not str(app.get("appid", "")).isdigit():
                continue
            aid = int(app["appid"])
            if is_compatibility_tool(app.get("name", "")):
                continue
            result[aid] = {"game_id": aid, "game_name": app.get("name", str(aid)),
                           "install_root": str(library), "install_dir": app.get("installdir", "")}
    return sorted(result.values(), key=lambda g: g["game_name"].casefold())


def game_directory(game):
    return Path(game["install_root"]) / "steamapps/common" / game.get("install_dir", "")


def running_games(items, ignore_registry_for=None):
    running = set()
    ignored = ({ignore_registry_for} if isinstance(ignore_registry_for, int)
               else set(ignore_registry_for or ()))
    if os.name == "nt":
        import winreg
        for g in items:
            if g["game_id"] in ignored:
                continue
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, rf"Software\Valve\Steam\Apps\{g['game_id']}") as key:
                    if winreg.QueryValueEx(key, "Running")[0]:
                        running.add(g["game_id"])
            except OSError:
                pass
    roots = [(g["game_id"], game_directory(g).resolve()) for g in items if g.get("install_dir")]
    for process in psutil.process_iter(["exe"]):
        try:
            exe = process.info["exe"]
            if exe:
                path = Path(exe)
                # Windows pseudo-processes can report names such as Registry
                # or MemCompression. Resolving those against Steam's game cwd
                # would turn them into false evidence of a running game.
                if not path.is_absolute():
                    continue
                path = path.resolve()
                running.update(aid for aid, root in roots if root in path.parents)
        except (psutil.Error, OSError):
            continue
    return running


def user_folders():
    home = Path.home()
    folders = {"Documents": home / "Documents", "Saved Games": home / "Saved Games",
               "Local": Path(os.environ.get("LOCALAPPDATA", home / "AppData/Local")),
               "Roaming": Path(os.environ.get("APPDATA", home / "AppData/Roaming")),
               "LocalLow": home / "AppData/LocalLow"}
    if os.name == "nt":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as key:
                for name, reg in [("Documents", "Personal"), ("Saved Games", "{4C5C32FF-BB9D-43B0-B5B4-2D72E54EAAA4}")]:
                    try:
                        folders[name] = Path(os.path.expandvars(winreg.QueryValueEx(key, reg)[0]))
                    except OSError:
                        pass
        except OSError:
            pass
    return folders


def candidates(root, account, game):
    """仅列出候选目录，用户确认后才成为备份来源。"""
    result = []
    remote = root / "userdata" / str(account) / str(game["game_id"]) / "remote"
    if remote.is_dir() and any(p.is_file() for p in remote.rglob("*")):
        result.append(remote)
    normalize = lambda s: re.sub(r"[^\w]", "", s).casefold()
    names = {normalize(game["game_name"]), normalize(game.get("install_dir", ""))} - {""}
    for base in user_folders().values():
        if not base.is_dir():
            continue
        try:
            first = list(base.iterdir())[:3000]
            for p in first:
                if p.is_dir() and not p.is_symlink():
                    if normalize(p.name) in names:
                        result.append(p)
                    elif p.name.lower() in {"my games", "games"}:
                        result.extend(c for c in p.iterdir() if c.is_dir() and normalize(c.name) in names)
        except OSError:
            continue
    return list(dict.fromkeys(str(p.resolve()) for p in result))
