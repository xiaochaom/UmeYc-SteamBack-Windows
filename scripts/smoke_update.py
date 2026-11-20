"""Windows CI: run the packaged updater, preserve portable state, restart EXE."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import psutil

from umeyc_steamback.updater import APP, EXE, PreparedUpdate, make_plan, _extract
from umeyc_steamback.protocol.custom import write_json


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    original = Path(sys.argv[1]).resolve()
    with tempfile.TemporaryDirectory(prefix="umeyc-update-smoke-") as temp:
        target = Path(temp) / "程序 目录 & test"
        data = target / "data"
        work = data / "updates/update-smoke"
        payload = work / "unpacked" / APP
        if len(sys.argv) > 2:
            assert _extract(Path(sys.argv[2]).resolve(), work / "unpacked") == payload
        else:
            shutil.copytree(original, payload)
        (target / EXE).write_bytes(b"old executable")
        (target / "_internal").mkdir()
        (target / "_internal/old.dll").write_bytes(b"old runtime")
        (data / "games.json").write_bytes(b"configuration must stay")
        (data / "quark").mkdir()
        (data / "quark/session.cookies").write_bytes(b"encrypted-session")
        (target / "backups").mkdir()
        (target / "backups/save.zip").write_bytes(b"backup must stay")
        parent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(2)"])
        worker = None
        try:
            plan_path = make_plan(PreparedUpdate("0.1.4", work, payload), target)
            plan = json.loads(plan_path.read_text())
            plan.update(pid=parent.pid, started=psutil.Process(parent.pid).create_time(), restart_args=["--smoke-test"])
            write_json(plan_path, plan)
            worker = subprocess.Popen([str(payload / EXE), "--apply-update", str(plan_path)], cwd=target)
            assert worker.wait(timeout=120) == 0, "Packaged updater failed"
            parent.wait(timeout=10)
            result = json.loads((data / "updates/result.json").read_text())
            assert result["ok"], result
            assert digest(target / EXE) == digest(original / EXE)
            assert not (target / "_internal/old.dll").exists()
            assert (data / "games.json").read_bytes() == b"configuration must stay"
            assert (data / "quark/session.cookies").read_bytes() == b"encrypted-session"
            assert (target / "backups/save.zip").read_bytes() == b"backup must stay"
            pid = json.loads((work / "restarted.json").read_text())["pid"]
            try:
                restarted = psutil.Process(pid)
            except psutil.NoSuchProcess:
                raise AssertionError("Restarted smoke process exited before its result could be observed")
            assert restarted.wait(timeout=30) == 0, "Updated EXE failed smoke test"
            print("Packaged updater replaced program, preserved state and restarted successfully")
        finally:
            if (work / "error.log").exists():
                print((work / "error.log").read_text(encoding="utf-8"))
            if worker is not None and worker.poll() is None:
                worker.kill()
                worker.wait()
            if parent.poll() is None:
                parent.kill()
                parent.wait()


if __name__ == "__main__":
    main()
