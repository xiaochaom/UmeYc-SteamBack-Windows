from types import SimpleNamespace

import pytest

from umeyc_steamback.cli import parse_args, run_steam_wrapper


def args():
    return parse_args(["--steam-launch", "--account", "123", "--game-id", "42", "--",
                       "C:/Game Folder/Game.exe", "--profile", "中文 玩家", "literal&arg"])


def test_wrapper_waits_for_sync_then_executes_original_command_and_reports_exit(tmp_path):
    events = []
    def connect(name, command, timeout):
        events.append(command["action"])
        return {"ok": True}
    def run(command):
        events.append(list(command))
        return SimpleNamespace(wait=lambda: 7)
    result = run_steam_wrapper(args(), tmp_path, connect=connect, run_game=run)
    assert events == ["prepare", args().command, "finished"]
    assert result == 7


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


def test_original_game_arguments_cannot_be_parsed_as_tool_flags():
    value = parse_args(["--steam-launch", "--account", "123", "--game-id", "42", "--", "game.exe", "--account", "999"])
    assert value.account == 123
    assert value.command == ["game.exe", "--account", "999"]
