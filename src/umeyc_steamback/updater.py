"""Download verified portable builds; a staged EXE replaces the stopped app."""
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler
from zipfile import ZipFile, BadZipFile

import psutil

from .protocol.custom import safe_file, write_json
from .updates import REPOSITORY_URL, version_number

APP = "UmeYc-SteamBack-Windows"
EXE = APP + ".exe"
PACKAGE = APP + ".zip"
APP_FILES = {EXE, "_internal", "LICENSE", "NOTICE", "README.md"}
MAX_DOWNLOAD = 512 * 1024**2
MAX_EXPANDED = 2 * 1024**3


@dataclass(frozen=True)
class PreparedUpdate:
    version: str
    work: Path
    payload: Path


def _hash(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _check_url(url):
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443)
            or parsed.hostname not in {"github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}):
        raise ValueError("更新下载地址无效")
    if parsed.hostname == "github.com" and not parsed.path.startswith("/xiaochaom/UmeYc-SteamBack-Windows/releases/download/"):
        raise ValueError("更新下载地址不属于本项目")


class _Redirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_download(url):
    _check_url(url)
    # A fresh opener, with no Quark cookies or credentials.
    return build_opener(_Redirect()).open(Request(url, headers={"User-Agent": APP}), timeout=20)


def _extract(archive, destination):
    with ZipFile(archive) as bundle:
        entries = bundle.infolist()
        if len(entries) > 30000 or sum(info.file_size for info in entries) > MAX_EXPANDED:
            raise ValueError("更新包解压大小超过限制")
        seen, targets = set(), []
        for info in entries:
            relative = info.filename[:-1] if info.is_dir() else info.filename
            target = safe_file(destination, relative)
            parts = Path(relative).parts
            if not parts or parts[0] != APP or (len(parts) == 1 and not info.is_dir()):
                raise ValueError("更新包目录结构无效")
            if len(parts) > 1 and parts[1] not in APP_FILES:
                raise ValueError("更新包包含非程序文件，未修改现有数据")
            if len(parts) > 2 and parts[1] != "_internal":
                raise ValueError("更新包程序文件结构无效")
            kind = stat.S_IFMT(info.external_attr >> 16)
            if kind not in (0, stat.S_IFREG, stat.S_IFDIR) or info.flag_bits & 1:
                raise ValueError("更新包包含链接或加密文件")
            identity = relative.casefold()
            if identity in seen:
                raise ValueError("更新包包含重复文件")
            seen.add(identity)
            targets.append((info, target))
        # All paths are checked before writing any files.
        for info, target in targets:
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(info) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output, length=1024**2)
    payload = destination / APP
    if not (payload / EXE).is_file() or not (payload / "_internal").is_dir():
        raise ValueError("更新包缺少程序或运行库")
    return payload


def download_update(result, data, progress=lambda received, total: None):
    version_number(result.version)
    if not result.available or (result.package_size is not None and not 0 < result.package_size <= MAX_DOWNLOAD):
        raise ValueError("没有可安装的新版安装包")
    folder = safe_file(data, "updates")
    folder.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="update-", dir=folder))
    base = REPOSITORY_URL + "/releases/download/v" + result.version + "/"
    try:
        with _open_download(base + "SHA256.txt") as response:
            checksum = response.read(16385)
        if len(checksum) > 16384:
            raise ValueError("更新校验文件过大")
        hashes = re.findall(r"^([a-fA-F0-9]{64})[ \t]+\*?" + re.escape(PACKAGE) + r"\s*$",
                            checksum.decode("utf-8-sig"), re.M)
        if len(hashes) != 1:
            raise ValueError("更新校验文件无效")
        archive = work / PACKAGE
        received = 0
        with _open_download(base + PACKAGE) as response, archive.open("wb") as output:
            while block := response.read(1024**2):
                received += len(block)
                if received > MAX_DOWNLOAD or (result.package_size and received > result.package_size):
                    raise ValueError("更新包大小异常")
                output.write(block)
                progress(received, result.package_size or 0)
        if not received or (result.package_size and received != result.package_size):
            raise ValueError("更新包下载不完整，请重试")
        if _hash(archive) != hashes[0].lower():
            raise ValueError("更新包校验失败，当前程序未修改")
        return PreparedUpdate(result.version, work, _extract(archive, work / "unpacked"))
    except BaseException as error:
        shutil.rmtree(work, ignore_errors=True)
        if isinstance(error, (URLError, TimeoutError)):
            raise ValueError("下载更新失败，请检查网络后重试") from None
        if isinstance(error, (UnicodeError, BadZipFile)):
            raise ValueError("更新包格式无效，当前程序未修改") from None
        raise


@contextmanager
def install_lock(data):
    path = Path(data) / "updates/install.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def update_in_progress(data):
    if not (Path(data) / "updates/install.lock").exists():
        return False
    try:
        with install_lock(data):
            return False
    except OSError:
        return True


def make_plan(prepared, target):
    target = Path(target).resolve()
    work = prepared.work.resolve()
    if work.parent != target / "data/updates" or prepared.payload.resolve() != work / "unpacked" / APP:
        raise ValueError("更新目录不属于当前程序")
    plan = work / "plan.json"
    write_json(plan, {"version": prepared.version, "target": str(target), "pid": os.getpid(),
                      "started": psutil.Process().create_time()})
    return plan


def can_self_update():
    return os.name == "nt" and getattr(sys, "frozen", False)


def launch_update(prepared, target):
    if not can_self_update():
        raise ValueError("应用内更新仅支持 Windows 打包版本；源码运行请使用 Git 更新")
    plan = make_plan(prepared, target)
    (prepared.work / "ready").unlink(missing_ok=True)
    if (prepared.work / "rollback").exists():
        raise ValueError("上次更新尚未清理，请重新下载更新包")
    worker = subprocess.Popen([str(prepared.payload / EXE), "--apply-update", str(plan)],
                              cwd=target, close_fds=True, creationflags=subprocess.CREATE_NO_WINDOW,
                              env={**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"})
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if (prepared.work / "ready").exists():
            return
        if worker.poll() is not None:
            raise ValueError("更新进程未能启动，当前程序未修改")
        time.sleep(.1)
    worker.terminate()
    worker.wait(timeout=5)
    raise ValueError("更新进程启动超时，当前程序未修改，请重试")


def _wait_for_parent(pid, started):
    if type(pid) is not int or pid <= 0 or pid == os.getpid():
        raise ValueError("更新进程编号无效")
    try:
        parent = psutil.Process(pid)
        if abs(parent.create_time() - started) < .01:
            parent.wait(timeout=120)
    except psutil.NoSuchProcess:
        pass


def _remove(path):
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def check_other_processes(target):
    exe = str((Path(target) / EXE).resolve()).casefold()
    for process in psutil.process_iter(["pid", "exe"]):
        try:
            if (process.pid != os.getpid() and process.info["exe"]
                    and str(Path(process.info["exe"]).resolve()).casefold() == exe):
                raise ValueError("还有游戏同步入口正在运行，请退出游戏并等待上传结束后更新")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue


def apply_update(plan_path, executable=None, restart=None):
    """Runs from the downloaded build, so its own runtime stays outside target."""
    plan_path = Path(plan_path).resolve()
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    version_number(plan["version"])
    target = Path(plan["target"]).resolve()
    work = plan_path.parent
    payload = work / "unpacked" / APP
    if work.parent != target / "data/updates" or plan_path.name != "plan.json":
        raise ValueError("更新计划路径无效")
    if executable is not None and Path(executable).resolve() != payload / EXE:
        raise ValueError("更新程序必须从已下载的更新目录运行")
    for node in payload.iterdir():
        if node.name not in APP_FILES:
            raise ValueError("更新包包含非程序文件")
    if not (payload / EXE).is_file() or not (payload / "_internal").is_dir():
        raise ValueError("更新包不完整")
    restart_args = plan.get("restart_args", [])
    if restart_args not in ([], ["--smoke-test"]):
        raise ValueError("更新重启参数无效")
    restart = restart or (lambda exe: subprocess.Popen([str(exe), *restart_args], cwd=target, close_fds=True,
                                                       env={**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"}))
    data = target / "data"
    with install_lock(data):
        (work / "ready").write_text("ready", encoding="ascii")
        # A timeout must not touch a running program or start a duplicate.
        _wait_for_parent(plan["pid"], plan["started"])
        rollback = work / "rollback"
        rollback.mkdir()
        old, installed = [], []
        success = True
        def restore_old():
            for name in reversed(installed):
                _remove(target / name)
            for name in reversed(old):
                os.replace(rollback / name, target / name)
        try:
            check_other_processes(target)
            for name in sorted(APP_FILES):
                path = safe_file(target, name)
                if path.exists():
                    os.replace(path, rollback / name)
                    old.append(name)
            for node in sorted(payload.iterdir()):
                destination = safe_file(target, node.name)
                installed.append(node.name)
                if node.is_dir():
                    shutil.copytree(node, destination)
                else:
                    shutil.copy2(node, destination)
        except Exception:
            restore_old()
            write_json(data / "updates/result.json", {"ok": False, "version": plan["version"]})
            success = False
        else:
            write_json(data / "updates/result.json", {"ok": True, "version": plan["version"]})
    # Release the install lock before the new/rolled-back app starts.
    try:
        process = restart(target / EXE)
    except OSError:
        if not success:
            raise
        with install_lock(data):
            restore_old()
            write_json(data / "updates/result.json", {"ok": False, "version": plan["version"]})
        process = restart(target / EXE)
        success = False
    if process is not None:
        write_json(work / "restarted.json", {"pid": process.pid})
    write_json(work / "done.json", {"pid": os.getpid(), "started": psutil.Process().create_time()})
    return success


def cleanup_updates(data):
    """The staged updater cannot delete its own loaded Windows runtime."""
    folder = Path(data) / "updates"
    for work in folder.glob("update-*"):
        done = work / "done.json"
        if not done.is_file():
            continue
        try:
            owner = json.loads(done.read_text(encoding="utf-8"))
            process = psutil.Process(owner["pid"])
            if abs(process.create_time() - owner["started"]) < .01:
                continue
        except psutil.NoSuchProcess:
            pass
        except (OSError, ValueError, KeyError, psutil.AccessDenied):
            continue
        shutil.rmtree(work, ignore_errors=True)
