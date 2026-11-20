"""Windows 程序入口和 Steam 账号校验；无需管理员权限。"""
import os
from pathlib import Path
import subprocess
import sys


def app_directory():
    """Portable data belongs beside the EXE, never in PyInstaller's temp tree."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def app_command(*args):
    if getattr(sys, "frozen", False):
        return [sys.executable, *args]
    executable = Path(sys.executable)
    windowless = executable.with_name("pythonw.exe")
    if os.name == "nt" and windowless.exists():
        executable = windowless
    return [str(executable), "-m", "umeyc_steamback", *args]


def active_steam_account():
    if os.name != "nt":
        return None
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam\ActiveProcess") as key:
            value = int(winreg.QueryValueEx(key, "ActiveUser")[0])
            return value if 0 < value < 2**32 else None
    except (OSError, ValueError):
        return None


def check_steam_account(account):
    active = active_steam_account()
    if active is not None and active != account:
        raise ValueError("Steam 当前登录账号与此游戏的监听账号不同，已暂停自动同步")


def refresh_executable_icon(executable):
    """Invalidate this EXE's Shell icon after replacement; leave Explorer running."""
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes
    try:
        notify = ctypes.windll.shell32.SHChangeNotify
        notify.argtypes = [wintypes.LONG, wintypes.UINT, ctypes.c_void_p, ctypes.c_void_p]
        notify.restype = None
        # SHCNE_UPDATEITEM + SHCNF_PATHW | SHCNF_FLUSH: refresh this file,
        # including cached executable icons, without clearing system caches.
        path = ctypes.c_wchar_p(str(Path(executable).resolve()))
        notify(0x2000, 0x0005 | 0x1000, ctypes.cast(path, ctypes.c_void_p), None)
        return True
    except (AttributeError, OSError):
        return False


def refresh_icon_once(data, executable, version):
    """Also refresh on first launch after manual upgrades or older updaters."""
    from .protocol.custom import write_json
    import json
    marker = Path(data) / "shell-icon.json"
    state = {"version": version, "executable": str(Path(executable).resolve())}
    try:
        if json.loads(marker.read_text(encoding="utf-8")) == state:
            return
    except (OSError, ValueError):
        pass
    if refresh_executable_icon(executable):
        try:
            write_json(marker, state)
        except OSError:
            pass
