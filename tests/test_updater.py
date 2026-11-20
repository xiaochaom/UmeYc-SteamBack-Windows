import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from zipfile import ZipFile, ZipInfo

import psutil
import pytest

from umeyc_steamback import updater as u
from umeyc_steamback.updates import UpdateResult
from umeyc_steamback.protocol.custom import write_json


def bundle(extra=None):
    buffer = io.BytesIO()
    with ZipFile(buffer, "w") as z:
        z.writestr(u.APP + "/" + u.EXE, b"MZ-new-program")
        z.writestr(u.APP + "/_internal/python.dll", b"new-runtime")
        z.writestr(u.APP + "/README.md", b"new-docs")
        for name, data in (extra or {}).items():
            z.writestr(name, data)
    return buffer.getvalue()


def serve(monkeypatch, payload, checksum=None):
    checksum = checksum or hashlib.sha256(payload).hexdigest().encode() + b"  " + u.PACKAGE.encode() + b"\n"
    urls = []
    def open_download(url):
        urls.append(url)
        return io.BytesIO(checksum if url.endswith("SHA256.txt") else payload)
    monkeypatch.setattr(u, "_open_download", open_download)
    return urls


def result(size=None):
    return UpdateResult("0.2.0", True, "https://untrusted.invalid/ignored", size)


def test_download_verifies_checksum_and_stages_only_inside_program_data(tmp_path, monkeypatch):
    payload = bundle()
    urls = serve(monkeypatch, payload)
    progress = []
    prepared = u.download_update(result(len(payload)), tmp_path / "data", lambda *p: progress.append(p))
    assert prepared.work.parent == tmp_path / "data/updates"
    assert (prepared.payload / u.EXE).read_bytes() == b"MZ-new-program"
    assert progress[-1] == (len(payload), len(payload))
    assert all(url.startswith(u.REPOSITORY_URL + "/releases/download/v0.2.0/") for url in urls)


@pytest.mark.parametrize("failure", ["checksum", "truncated", "oversize", "offline"])
def test_failed_download_cleans_partial_files_without_touching_current_app(tmp_path, monkeypatch, failure):
    payload = bundle()
    exe = tmp_path / u.EXE
    exe.write_bytes(b"old")
    if failure == "offline":
        from urllib.error import URLError
        def offline(_):
            raise URLError("offline")
        monkeypatch.setattr(u, "_open_download", offline)
    else:
        serve(monkeypatch, payload, b"0" * 64 + b"  " + u.PACKAGE.encode() if failure == "checksum" else None)
    expected_size = len(payload) + (1 if failure == "truncated" else -1 if failure == "oversize" else 0)
    with pytest.raises(ValueError):
        u.download_update(result(expected_size), tmp_path / "data")
    assert exe.read_bytes() == b"old"
    assert not list((tmp_path / "data/updates").glob("update-*"))


@pytest.mark.parametrize("name", ["../escape", u.APP + "/../escape", u.APP + "/data/games.json",
    u.APP + "/backups/save.zip", u.APP + "/_internal/C:evil", u.APP + "/_internal/CON",
    u.APP + "/_internal/../escape", "OtherTool/tool.exe", u.APP + "/_internal/PYTHON.DLL"])
def test_unsafe_update_zip_is_rejected_before_installing(tmp_path, monkeypatch, name):
    payload = bundle({name: b"bad"})
    serve(monkeypatch, payload)
    with pytest.raises(ValueError):
        u.download_update(result(len(payload)), tmp_path / "data")
    assert not (tmp_path / "escape").exists()
    assert not list((tmp_path / "data/updates").glob("update-*"))


def test_symlink_in_update_archive_is_rejected(tmp_path, monkeypatch):
    buffer = io.BytesIO(bundle())
    with ZipFile(buffer, "a") as z:
        entry = ZipInfo(u.APP + "/_internal/link")
        entry.create_system = 3
        entry.external_attr = 0o120777 << 16
        z.writestr(entry, "../../data")
    serve(monkeypatch, buffer.getvalue())
    with pytest.raises(ValueError, match="链接"):
        u.download_update(result(), tmp_path / "data")


@pytest.mark.parametrize("url", ["http://github.com/file", "https://evil.invalid/file", "https://github.com/other/repo/file",
    "https://github.com:8080/xiaochaom/UmeYc-SteamBack-Windows/releases/download/v0.2.0/file",
    "https://user:secret@github.com/xiaochaom/UmeYc-SteamBack-Windows/releases/download/v0.2.0/file"])
def test_update_redirect_rejects_untrusted_destinations(url):
    with pytest.raises(ValueError):
        u._check_url(url)


@pytest.fixture
def staged(tmp_path, monkeypatch):
    target = tmp_path / "应用 空格 & 符号"
    data = target / "data"
    data.mkdir(parents=True)
    (data / "games.json").write_bytes(b"configuration")
    (data / "quark").mkdir()
    (data / "quark/session.cookies").write_bytes(b"encrypted-cookie")
    (target / "backups/123/42").mkdir(parents=True)
    (target / "backups/123/42/save.zip").write_bytes(b"save")
    (target / u.EXE).write_bytes(b"old-program")
    (target / "_internal").mkdir()
    (target / "_internal/old.dll").write_bytes(b"old-runtime")
    (target / "custom.txt").write_bytes(b"unrelated-file")
    payload = bundle()
    serve(monkeypatch, payload)
    prepared = u.download_update(result(len(payload)), data)
    plan = u.make_plan(prepared, target)
    waited = []
    monkeypatch.setattr(u, "_wait_for_parent", lambda *p: waited.append(p))
    return target, data, prepared, plan, waited


def check_data(target, data):
    assert (data / "games.json").read_bytes() == b"configuration"
    assert (data / "quark/session.cookies").read_bytes() == b"encrypted-cookie"
    assert (target / "backups/123/42/save.zip").read_bytes() == b"save"
    assert (target / "custom.txt").read_bytes() == b"unrelated-file"


def test_apply_waits_exits_replaces_whole_runtime_and_preserves_data(staged):
    target, data, prepared, plan, waited = staged
    restarted = []
    def restart(exe):
        assert not u.update_in_progress(data)
        restarted.append(exe)
    assert u.apply_update(plan, restart=restart)
    assert waited and restarted == [target / u.EXE]
    assert (target / u.EXE).read_bytes() == b"MZ-new-program"
    assert (target / "_internal/python.dll").read_bytes() == b"new-runtime"
    assert not (target / "_internal/old.dll").exists()
    assert json.loads((data / "updates/result.json").read_text())["ok"]
    check_data(target, data)


@pytest.mark.parametrize("failure", ["old_locked", "copy_runtime", "restart"])
def test_failed_install_restores_old_program_and_restarts_it(staged, monkeypatch, failure):
    target, data, prepared, plan, waited = staged
    if failure == "old_locked":
        original = os.replace
        def replace(source, destination):
            if Path(source) == target / "_internal":
                raise PermissionError("file in use")
            return original(source, destination)
        monkeypatch.setattr(os, "replace", replace)
    elif failure == "copy_runtime":
        original = shutil.copytree
        def copy(source, destination, *args, **kwargs):
            if Path(source) == prepared.payload / "_internal":
                Path(destination).mkdir()
                (Path(destination) / "partial.dll").write_bytes(b"partial")
                raise OSError("disk full")
            return original(source, destination, *args, **kwargs)
        monkeypatch.setattr(shutil, "copytree", copy)
    restarted = []
    def restart(exe):
        restarted.append(exe)
        assert not u.update_in_progress(data)
        if failure == "restart" and len(restarted) == 1:
            raise OSError("cannot launch new executable")
    assert not u.apply_update(plan, restart=restart)
    assert restarted[-1] == target / u.EXE
    assert (target / u.EXE).read_bytes() == b"old-program"
    assert (target / "_internal/old.dll").read_bytes() == b"old-runtime"
    assert not (target / "_internal/python.dll").exists()
    assert not json.loads((data / "updates/result.json").read_text())["ok"]
    check_data(target, data)


def test_parent_timeout_never_changes_or_restarts_live_program(staged, monkeypatch):
    target, data, prepared, plan, waited = staged
    def timeout(*_):
        raise psutil.TimeoutExpired(120)
    monkeypatch.setattr(u, "_wait_for_parent", timeout)
    with pytest.raises(psutil.TimeoutExpired):
        u.apply_update(plan, restart=lambda _: pytest.fail("parent is still running"))
    assert (target / u.EXE).read_bytes() == b"old-program"
    assert not (prepared.work / "rollback").exists()
    assert not u.update_in_progress(data)
    check_data(target, data)


def test_cross_directory_update_plan_is_rejected(staged, tmp_path):
    target, data, prepared, plan, waited = staged
    saved = json.loads(plan.read_text())
    saved["target"] = str(tmp_path / "other-application")
    write_json(plan, saved)
    with pytest.raises(ValueError, match="路径"):
        u.apply_update(plan, restart=lambda _: pytest.fail("invalid plan"))
    assert not waited
    check_data(target, data)


def test_waits_for_exact_parent_process_then_continues():
    parent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(.3)"])
    try:
        started = psutil.Process(parent.pid).create_time()
        u._wait_for_parent(parent.pid, started)
        assert parent.poll() is not None
    finally:
        if parent.poll() is None:
            parent.kill()
        parent.wait()


def test_install_lock_blocks_another_process(tmp_path):
    code = "from umeyc_steamback.updater import update_in_progress; import sys; print(update_in_progress(sys.argv[1]))"
    with u.install_lock(tmp_path):
        assert u.update_in_progress(tmp_path)
        result = subprocess.run([sys.executable, "-c", code, str(tmp_path)], capture_output=True, text=True, check=True)
        assert result.stdout.strip() == "True"
    assert not u.update_in_progress(tmp_path)
