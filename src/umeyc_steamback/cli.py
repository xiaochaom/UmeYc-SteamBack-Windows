"""Steam 启动包装器仅用 QCoreApplication，无主窗口。"""
import argparse
import os
import subprocess
import sys
import time
import json
import uuid

import psutil
from .storage import data_directory

WRAPPER_CONTEXT = "UMEYC_STEAMBACK_LAUNCH_SESSION"


def inherited_wrapper(account, game_id):
    """Only a live ancestor for this game can own a nested launch entry."""
    try:
        owner = json.loads(os.environ.get(WRAPPER_CONTEXT, "null"))
        if not isinstance(owner, dict) or owner.get("account") != account or owner.get("game_id") != game_id:
            return None
        from .ipc import valid_launch_command
        if not valid_launch_command(owner) or "session" not in owner:
            return None
        if any(p.pid == owner["pid"] and abs(p.create_time() - owner["started"]) < .01
               for p in psutil.Process().parents()):
            return owner
    except (ValueError, TypeError, psutil.Error):
        pass
    return None


def parse_args(argv):
    parser = argparse.ArgumentParser(description="UmeYc-SteamBack-Windows")
    parser.add_argument("--background", action="store_true")
    parser.add_argument("--session-host", action="store_true")
    parser.add_argument("--smoke-test", action="store_true")
    parser.add_argument("--apply-update")
    parser.add_argument("--steam-launch", action="store_true")
    parser.add_argument("--account", type=int)
    parser.add_argument("--game-id", type=int)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.steam_launch:
        if not all(v is not None and 0 < v < 2**32 for v in (args.account, args.game_id)):
            parser.error("Steam 启动需要有效账号与游戏 ID")
        if not args.command or args.command[0] != "--" or len(args.command) < 2:
            parser.error("缺少 Steam 原始启动命令，请检查 %command%")
        args.command = args.command[1:]
    return args


def run_steam_wrapper(args, data, connect=None, start_host=None, run_game=None, wait=None):
    from .desktop import app_command, check_steam_account
    from .ipc import request, server_name
    connect = connect or request
    wait = wait or time.sleep
    inherited = inherited_wrapper(args.account, args.game_id)
    command = inherited or {"action": "prepare", "account": args.account, "game_id": args.game_id,
                            "session": uuid.uuid4().hex, "pid": os.getpid(), "started": psutil.Process().create_time()}
    run_game = run_game or (lambda argv: subprocess.Popen(argv, shell=False,
                            env={**os.environ, WRAPPER_CONTEXT: json.dumps(command)}))
    start_host = start_host or (lambda: subprocess.Popen(app_command("--background", "--session-host"), close_fds=True))
    check_steam_account(args.account)
    if inherited:
        # A duplicated Steam prefix or a loader may invoke the entry again.
        # The outer wrapper already synchronized and owns the final upload.
        return run_game(args.command).wait()
    name = server_name(data)
    result = connect(name, command, 30 * 60 * 1000)
    if result is None:
        start_host()
        for _ in range(30):
            wait(.5)
            result = connect(name, command, 30 * 60 * 1000)
            if result is not None:
                break
        else:
            raise ValueError("后台程序未能启动，游戏尚未运行；请打开本工具检查")
    if not result.get("ok"):
        raise ValueError(result.get("error", "启动前云同步失败，游戏未启动"))
    check_steam_account(args.account)
    # Execute Steam's exact argv with inherited working directory/environment.
    # Calling a Steam URL here would recursively invoke this wrapper.
    try:
        process = run_game(args.command)
        return process.wait()
    finally:
        try:
            finished = {**command, "action": "finished"}
            result = connect(name, finished, 2000)
            if result is None:
                # The user may have closed the management window while the
                # game was running. Wake a new host only for the exit upload.
                start_host()
                for _ in range(30):
                    wait(.5)
                    if connect(name, finished, 2000) is not None:
                        break
        except (OSError, ValueError):
            pass  # The observer also detects game exits and retries uploads.


def main():
    args = parse_args(sys.argv[1:])
    if args.apply_update:
        from .updater import apply_update
        try:
            return 0 if apply_update(args.apply_update, executable=sys.executable) else 1
        except Exception:
            import traceback
            from pathlib import Path
            try:
                (Path(args.apply_update).parent / "error.log").write_text(traceback.format_exc(), encoding="utf-8")
            except OSError:
                pass
            if os.name == "nt":
                import ctypes
                ctypes.windll.user32.MessageBoxW(None, "更新未完成，请重新打开工具。若程序无法启动，请保留 data 和 backups，重新解压正式安装包；旧程序文件保留在 data\\updates 的 rollback 目录。",
                                                "UmeYc-SteamBack 更新", 0x10)
            return 1
    if not args.steam_launch:
        from .app import main as gui_main
        return gui_main()
    from PySide6.QtCore import QCoreApplication
    core = QCoreApplication.instance() or QCoreApplication(sys.argv)
    try:
        from .updater import update_in_progress
        if update_in_progress(data_directory()):
            raise ValueError("工具正在更新，请完成后再启动游戏")
        return run_steam_wrapper(args, data_directory())
    except Exception as error:
        text = str(error) if isinstance(error, (OSError, ValueError)) else "同步启动失败，请打开工具检查"
        if os.name == "nt":
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, text + "\n游戏未启动。请打开工具处理云存档或关闭自动云同步后重试。",
                                           "UmeYc-SteamBack 启动前同步", 0x10)
        else:
            print(text, file=sys.stderr)
        return 1
