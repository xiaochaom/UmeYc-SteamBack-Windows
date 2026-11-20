import ctypes
import json
from pathlib import Path
from types import SimpleNamespace

from umeyc_steamback import desktop


def test_refresh_icon_once_retries_failure_and_handles_upgrade_or_move(tmp_path, monkeypatch):
    calls = []
    executable = tmp_path / "程序/UmeYc-SteamBack-Windows.exe"
    data = tmp_path / "data"
    monkeypatch.setattr(desktop, "refresh_executable_icon", lambda exe: False)
    desktop.refresh_icon_once(data, executable, "0.1.7")
    assert not (data / "shell-icon.json").exists()
    monkeypatch.setattr(desktop, "refresh_executable_icon", lambda exe: calls.append(exe) or True)
    desktop.refresh_icon_once(data, executable, "0.1.7")
    desktop.refresh_icon_once(data, executable, "0.1.7")
    assert calls == [executable]
    desktop.refresh_icon_once(data, executable, "0.1.8")
    moved = tmp_path / "新目录/UmeYc-SteamBack-Windows.exe"
    desktop.refresh_icon_once(data, moved, "0.1.8")
    assert calls == [executable, executable, moved]
    assert json.loads((data / "shell-icon.json").read_text()) == {
        "version": "0.1.8", "executable": str(moved.resolve())}


def test_shell_refresh_targets_only_the_executable_without_restarting_explorer(tmp_path, monkeypatch):
    calls = []
    def notify(event, flags, path, second):
        calls.append((event, flags, ctypes.wstring_at(path), second))
    monkeypatch.setattr(desktop, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(shell32=SimpleNamespace(SHChangeNotify=notify)), raising=False)
    exe = tmp_path / "程序/UmeYc-SteamBack-Windows.exe"
    assert desktop.refresh_executable_icon(exe)
    assert calls == [(0x2000, 0x1005, str(exe.resolve()), None)]


def test_unavailable_shell_does_not_prevent_startup(tmp_path, monkeypatch):
    def notify(*_):
        raise OSError("Shell unavailable")
    monkeypatch.setattr(desktop, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(shell32=SimpleNamespace(SHChangeNotify=notify)), raising=False)
    desktop.refresh_icon_once(tmp_path / "data", tmp_path / "app.exe", "0.1.7")
    assert not (tmp_path / "data/shell-icon.json").exists()
