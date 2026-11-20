"""Windows 程序入口和 Steam 账号校验；无需管理员权限。"""
import os
from pathlib import Path
import subprocess
import sys

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
