from types import SimpleNamespace
import json
import os
import subprocess
import sys

import psutil

import pytest

from umeyc_steamback.cli import parse_args, run_steam_wrapper


def args():
    return parse_args(["--steam-launch", "--account", "123", "--game-id", "42", "--",
                       "C:/Game Folder/Game.exe", "--profile", "中文 玩家", "literal&arg"])


def test_wrapper_waits_for_sync_then_executes_original_command_and_reports_exit(tmp_path):
    events = []
    requests = []
    def connect(name, command, timeout):
        events.append(command["action"])
        requests.append(command)
        return {"ok": True}
    def run(command):
        events.append(list(command))
        return SimpleNamespace(wait=lambda: 7)
    result = run_steam_wrapper(args(), tmp_path, connect=connect, run_game=run)
    assert events == ["prepare", args().command, "finished"]
    assert result == 7
    assert requests[0]["session"] == requests[1]["session"]
    assert requests[0]["pid"] == os.getpid()
    assert requests[0]["started"] == psutil.Process().create_time()


def test_nested_entry_from_live_parent_runs_game_without_a_second_sync(tmp_path):
    from umeyc_steamback.cli import WRAPPER_CONTEXT
    owner = {"action": "prepare", "account": 123, "game_id": 42, "session": "a" * 32,
             "pid": os.getpid(), "started": psutil.Process().create_time()}
    code = """
from types import SimpleNamespace
from umeyc_steamback.cli import parse_args, run_steam_wrapper
import sys
args = parse_args(['--steam-launch', '--account', '123', '--game-id', '42', '--', 'game.exe'])
def forbidden(*args):
    raise AssertionError('Nested entry must not create a second session')
result = run_steam_wrapper(args, sys.argv[1], connect=forbidden, start_host=forbidden,
                          run_game=lambda command: SimpleNamespace(wait=lambda: 7))
assert result == 7
"""
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path)], capture_output=True,
                            env={**os.environ, WRAPPER_CONTEXT: json.dumps(owner)})
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("change", ["account", "game_id", "started", "pid"])
def test_stale_or_unrelated_inherited_context_cannot_skip_sync(monkeypatch, change):
    from umeyc_steamback.cli import inherited_wrapper, WRAPPER_CONTEXT
    owner = {"action": "prepare", "account": 123, "game_id": 42, "session": "a" * 32,
             "pid": os.getppid(), "started": psutil.Process(os.getppid()).create_time()}
    owner[change] += 100
    monkeypatch.setenv(WRAPPER_CONTEXT, json.dumps(owner))
    assert inherited_wrapper(123, 42) is None


def test_failed_preflight_never_starts_game(tmp_path):
    with pytest.raises(ValueError, match="冲突"):
        run_steam_wrapper(args(), tmp_path,
                          connect=lambda *_: {"ok": False, "error": "云存档冲突"},
                          run_game=lambda _: pytest.fail("must not launch"))


def test_wrapper_bootstraps_host_only_when_needed(tmp_path):
    attempts, events = [], []
    def connect(*_):
        attempts.append(1)
        return None if len(attempts) < 3 else {"ok": True}
    run_steam_wrapper(args(), tmp_path, connect=connect, start_host=lambda: events.append("host"),
                      wait=lambda _: None, run_game=lambda _: SimpleNamespace(wait=lambda: 0))
    assert events == ["host"]


def test_wrapper_wakes_exit_upload_host_when_user_closed_management(tmp_path):
    events, attempts = [], []
    def connect(name, command, timeout):
        events.append(command["action"])
        if command["action"] == "finished":
            attempts.append(1)
            if len(attempts) < 3:
                return None
        return {"ok": True}
    def run(command):
        events.append("game")
        return SimpleNamespace(wait=lambda: 7)
    result = run_steam_wrapper(args(), tmp_path, connect=connect,
                              start_host=lambda: events.append("host"), wait=lambda _: None, run_game=run)
    assert result == 7
    assert events == ["prepare", "game", "finished", "host", "finished", "finished"]


def test_original_game_arguments_cannot_be_parsed_as_tool_flags():
    value = parse_args(["--steam-launch", "--account", "123", "--game-id", "42", "--", "game.exe", "--account", "999"])
    assert value.account == 123
    assert value.command == ["game.exe", "--account", "999"]


@pytest.mark.parametrize("change", [
    {"session": "invalid"}, {"pid": True}, {"pid": -1}, {"started": float("nan")},
    {"started": "1"}, {"account": False}, {"unexpected": 1},
])
def test_ipc_rejects_invalid_launch_ownership(change):
    from umeyc_steamback.ipc import valid_launch_command
    command = {"action": "prepare", "account": 123, "game_id": 42,
               "session": "a" * 32, "pid": os.getpid(), "started": psutil.Process().create_time()}
    assert valid_launch_command(command)
    assert not valid_launch_command({**command, **change})
