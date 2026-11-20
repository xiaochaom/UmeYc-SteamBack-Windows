"""可移植的云存档版本；完整发布后只保留最新一份云端存档。"""
import asyncio
import copy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import threading
import time
import uuid
from zipfile import ZipFile, ZIP_DEFLATED, BadZipFile

from .custom import safe_file, write_json
from .quark import QuarkClient, QuarkError, LoginExpired

MAX_BYTES = 2 * 1024**3
MAX_FILES = 50000
COMMIT = re.compile(r"version-\d{13}-[a-f0-9]{32}\.json")
ARCHIVE = re.compile(r"version-\d{13}-[a-f0-9]{32}\.zip")


class CloudConflict(ValueError):
    def __init__(self, remote):
        super().__init__("云端存在尚未同步的不同版本，请确认后再上传")
        self.remote = remote


def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024**2), b""):
            h.update(chunk)
    return h.hexdigest()


def source_plan(engine, game):
    info = engine._custom_plan(game)
    mode = info.get("portable_mode", "custom") if info is not None else "auto"
    if info is None:
        info = dict(game)
        info.pop("save_games_roots", None)
        rcf = engine._read_rcf(info)
        if not rcf:
            raise ValueError("无法识别存档位置，请先选择存档路径")
        files = {suffix: rcf for suffix in info["save_games_roots"].values()}
    else:
        files = info["custom_files"]
    return info, files, mode


def root_keys(engine, info, mode):
    if "portable_keys" in info:
        return list(info["portable_keys"])
    keys = []
    for index, root in enumerate(info["save_games_roots"]):
        if mode == "custom":
            keys.append(f"custom-{index}")
            continue
        normalized = Path(root).as_posix()
        if "/pfx/" in normalized:
            key = "proton/" + normalized.split("/pfx/", 1)[1]
        elif "/userdata/" in normalized:
            tail = normalized.split("/userdata/", 1)[1].split("/", 1)
            key = "userdata/" + tail[-1]
        else:
            try:
                key = "home/" + str(Path(root).relative_to(Path.home()))
            except ValueError:
                key = "game/" + Path(root).name
        keys.append(key)
    return keys


def pack_current(engine, game, destination):
    info, file_map, mode = source_plan(engine, game)
    manifest = {"format": 1, "game_id": int(game["game_id"]), "steam_account": str(next(iter(engine.account_ids))),
                "mode": mode, "root_keys": root_keys(engine, info, mode), "files": []}
    total, latest = 0, None
    before = []
    with ZipFile(destination, "w", compression=ZIP_DEFLATED, compresslevel=6) as archive:
        for slot, (root, suffix) in enumerate(info["save_games_roots"].items()):
            for relative in sorted(set(file_map.get(suffix, []))):
                path = safe_file(root, relative)
                if not path.is_file():
                    continue
                old = path.stat()
                total += old.st_size
                if total > MAX_BYTES or len(manifest["files"]) >= MAX_FILES:
                    raise ValueError("存档超过 2 GiB 或文件数量过多，请缩小所选范围")
                relative_posix = Path(relative).as_posix()
                name = f"files/{slot}/{relative_posix}"
                h = hashlib.sha256()
                with path.open("rb") as src, archive.open(name, "w") as dst:
                    for chunk in iter(lambda: src.read(1024**2), b""):
                        dst.write(chunk); h.update(chunk)
                before.append((path, old.st_size, old.st_mtime_ns))
                latest = old.st_mtime_ns // 1000000 if latest is None else max(latest, old.st_mtime_ns // 1000000)
                manifest["files"].append({"slot": slot, "path": relative_posix, "size": old.st_size,
                    "mtime_ns": old.st_mtime_ns, "sha256": h.hexdigest()})
        if not manifest["files"]:
            raise ValueError("当前没有可同步的存档文件")
        # Re-enumerate too: new slots created during packaging must not be lost.
        new_info, new_map, new_mode = source_plan(engine, game)
        if info["save_games_roots"] != new_info["save_games_roots"] or file_map != new_map or mode != new_mode:
            raise ValueError("存档在打包时发生变化，请退出游戏后重试")
        if any(not p.exists() or (p.stat().st_size, p.stat().st_mtime_ns) != (s, m) for p, s, m in before):
            raise ValueError("存档在打包时发生变化，请退出游戏后重试")
        manifest["save_time"] = latest
        content = [[f["slot"], f["path"], f["size"], f["sha256"]] for f in manifest["files"]]
        manifest["content_hash"] = hashlib.sha256(json.dumps([manifest["root_keys"], content], ensure_ascii=False).encode()).hexdigest()
        encoded = json.dumps(manifest, ensure_ascii=False).encode()
        if len(encoded) > 8 * 1024**2:
            raise ValueError("存档文件清单过大，请缩小所选范围")
        archive.writestr("manifest.json", encoded)
    return manifest


def unpack_verified(archive_path, staging, game, account, expected_hash):
    """逐项验证名称、类型、大小、内容摘要，不按云端绝对路径解压。"""
    staging = Path(staging).resolve()
    with ZipFile(archive_path) as archive:
        entries = archive.infolist()
        if len(entries) > MAX_FILES + 1 or len({e.filename for e in entries}) != len(entries):
            raise ValueError("云存档包含重复文件或文件数量过多")
        for e in entries:
            p = PurePosixPath(e.filename)
            if p.is_absolute() or ".." in p.parts or "\\" in e.filename or stat.S_ISLNK(e.external_attr >> 16):
                raise ValueError("云存档包含无效路径或符号链接")
        try:
            header = archive.getinfo("manifest.json")
        except KeyError:
            raise ValueError("云存档缺少文件清单") from None
        if header.file_size > 8 * 1024**2 or sum(e.file_size for e in entries) > MAX_BYTES + 8 * 1024**2:
            raise ValueError("云存档解压大小超过限制")
        m = json.loads(archive.read("manifest.json"))
        if m.get("format") != 1 or m.get("game_id") != int(game["game_id"]) or m.get("steam_account") != str(account):
            raise ValueError("云存档不属于当前 Steam 账号或游戏")
        keys = m.get("root_keys")
        files = m.get("files")
        if not isinstance(keys, list) or not keys or len(keys) > 100 or not isinstance(files, list) or not files:
            raise ValueError("云存档文件清单无效")
        expected_names = {"manifest.json"}
        windows_names = set()
        content = []
        for f in files:
            slot, relative = f["slot"], f["path"]
            if type(slot) is not int or not 0 <= slot < len(keys) or not isinstance(relative, str):
                raise ValueError("云存档目录映射无效")
            path = safe_file(Path(staging) / str(slot), relative)
            if "\\" in relative or str(PurePosixPath(relative)) != relative:
                raise ValueError("云存档文件路径无效")
            name = f"files/{slot}/{relative}"
            if name in expected_names or name.casefold() in windows_names:
                raise ValueError("云存档包含重复文件")
            windows_names.add(name.casefold())
            expected_names.add(name)
            if type(f["size"]) is not int or archive.getinfo(name).file_size != f["size"]:
                raise ValueError("云存档文件大小不符")
            if type(f["mtime_ns"]) is not int or not 0 <= f["mtime_ns"] < 2**63:
                raise ValueError("云存档时间无效")
            path.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(name) as src, path.open("wb") as dst:
                shutil.copyfileobj(src, dst, 1024**2)
            if digest(path) != f["sha256"]:
                raise ValueError("云存档文件校验失败，未覆盖本地存档")
            os.utime(path, ns=(f["mtime_ns"], f["mtime_ns"]))
            content.append([slot, relative, f["size"], f["sha256"]])
        if expected_names != {e.filename for e in entries}:
            raise ValueError("云存档含未登记文件")
        calculated = hashlib.sha256(json.dumps([keys, content], ensure_ascii=False).encode()).hexdigest()
        if calculated != m.get("content_hash") or calculated != expected_hash:
            raise ValueError("云存档内容校验失败")
        return m


class CloudService:
    def __init__(self, engine, client=None):
        self.engine = engine
        self.client = client or QuarkClient(engine.config.app_data_dir)
        self.lock = threading.RLock()
        self.state_path = Path(engine.config.app_data_dir) / "cloud_settings.json"
        self.activity_path = Path(engine.config.app_data_dir) / "cloud_activity.json"
        self.jobs = {}
        self.tasks = set()
        self.running = set()
        self.uid = self.client.user_id
        self.status_cache = {}
        self.job_gate = asyncio.Lock()
        self.active_account = None

    def isolated_engine(self, account):
        if type(account) is not int or not 0 < account < 2**32:
            raise ValueError("Steam 账号尚未加载，请刷新")
        engine = type(self.engine)(self.engine.config)
        engine.account_ids = {account}
        return engine

    def read_state(self, path=None):
        p = path or self.state_path
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    def key(self, account, game):
        return f"{self.uid}:{account}:{int(game['game_id'])}"

    def account_status(self):
        with self.lock:
            try:
                state = self.client.account()
                self.uid = state["user_id"]
                return state
            except LoginExpired as e:
                self.uid = None
                return {"logged_in": False, "user_id": None, "error": str(e)}
            except QuarkError as e:
                return {"logged_in": bool(self.uid), "user_id": self.uid, "error": str(e)}

    def set_auto(self, account, game, enabled):
        with self.lock:
            if not isinstance(enabled, bool):
                raise ValueError("无效的自动上传开关")
            if not self.account_status()["logged_in"]:
                raise ValueError("请先登录夸克")
            state = self.read_state()
            entry = state.setdefault(self.key(account, game), {})
            entry.update(auto_upload=enabled, game={k: game[k] for k in ("game_id", "game_name", "install_root")})
            write_json(self.state_path, state)
            return enabled

    def directory(self, account, game, create=False):
        folder = "0"
        for name in ("UmeYc-SteamBack", str(account), str(int(game["game_id"]))):
            folder = self.client.folder(folder, name, create)
            if folder is None:
                return None
        return folder

    def latest(self, account, game):
        folder = self.directory(account, game)
        if folder is None:
            return None
        commits = [f for f in self.client.list_files(folder) if COMMIT.fullmatch(f["file_name"])]
        if not commits:
            return None
        # Prefer server creation time to device clocks; filename breaks ties.
        entry = max(commits, key=lambda x: (int(x.get("created_at", 0)), x["file_name"]))
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp) / "commit.json"
            self.client.download(entry["fid"], p, limit=1024**2)
            record = json.loads(p.read_text(encoding="utf-8"))
        if record.get("format") != 1 or record.get("game_id") != int(game["game_id"]) or record.get("steam_account") != str(account):
            raise ValueError("云端索引不属于当前账号或游戏")
        stem = entry["file_name"].removesuffix(".json")
        if record.get("archive_name") != stem + ".zip":
            raise ValueError("云端存档索引无效")
        for field in ("archive_hash", "content_hash"):
            if not re.fullmatch(r"[a-f0-9]{64}", str(record.get(field, ""))):
                raise ValueError("云端存档索引校验值无效")
        record["revision"] = entry["fid"]
        return record

    def status(self, account, game):
        key = self.key(account, game)
        job = next((dict(j) for j in reversed(list(self.jobs.values()))
                    if j["account"] == account and j["game_id"] == int(game["game_id"]) and j.get("uid") == self.uid), None)
        if job and job["state"] == "running":
            saved = self.read_state().get(key, {})
            return {"logged_in": bool(self.uid), "user_id": self.uid, "auto_upload": False,
                    **self.status_cache.get(key, {}), "auto_upload": saved.get("auto_upload", False), "activity": job}
        with self.lock:
            self.isolated_engine(account)
            auth = self.account_status()
            key = self.key(account, game)
            entry = self.read_state().get(key, {})
            activity = job or self.read_state(self.activity_path).get(key)
            result = {**auth, "auto_upload": entry.get("auto_upload", False), "activity": activity,
                      "cloud_time": None, "uploaded_at": None, "revision": None}
            if not auth["logged_in"] or auth.get("error"):
                return result
            try:
                remote = self.latest(account, game)
                if remote:
                    result.update(cloud_time=remote["save_time"], uploaded_at=remote["uploaded_at"], revision=remote["revision"])
            except (ValueError, OSError, KeyError) as error:
                result["error"] = str(error) if isinstance(error, ValueError) else "云存档读取失败，请重试"
            self.status_cache[key] = result
            return result

    def remember(self, account, game, content_hash):
        state = self.read_state()
        state.setdefault(self.key(account, game), {})["synced_hash"] = content_hash
        write_json(self.state_path, state)

    def _prune_versions(self, account, game, folder, before, retained):
        """只清理同步前已存在的旧文件，保护其他设备正在上传的新文件。"""
        warning = "存档已同步，但旧云存档暂未清理完成，下次上传时会重试"
        try:
            latest = self.latest(account, game)
            if not latest or latest["revision"] != retained["revision"]:
                return "云端版本已变化，已跳过旧版本清理，请刷新后重试"
            listed = self.client.list_files(folder)
            # The retained index must point to a complete archive in this folder.
            # Never delete an old version if the new pair isn't actually visible.
            if not any(f["fid"] == retained["archive_fid"] and f["file_name"] == retained["archive_name"]
                       and not QuarkClient.is_directory(f) and int(f.get("size", 0)) > 0 for f in listed):
                return warning
            baseline = {f["fid"]: f for f in before if not QuarkClient.is_directory(f)}
            old_commits = {f["fid"]: f for f in listed
                           if f["fid"] in baseline and f["file_name"] == baseline[f["fid"]]["file_name"]
                           and COMMIT.fullmatch(f["file_name"]) and f["fid"] != retained["revision"]}
            old_stems = {f["file_name"].removesuffix(".json") for f in old_commits.values()}

            def remove(files):
                ids = list(files)
                for offset in range(0, len(ids), 100):
                    current = self.latest(account, game)
                    if not current or current["revision"] != retained["revision"]:
                        raise ValueError("云端版本已变化")
                    batch = ids[offset:offset + 100]
                    self.client.delete_files(batch)
                    for attempt in range(6):
                        visible = {f["fid"] for f in self.client.list_files(folder)}
                        if not visible.intersection(batch):
                            break
                        if attempt < 5:
                            time.sleep(1)
                    else:
                        raise ValueError("夸克尚未确认旧版本删除完成")

            # Remove obsolete indexes before their archives. If deletion fails,
            # leave those archives in place and retry on the next upload.
            remove(old_commits)
            listed = self.client.list_files(folder)
            live_stems = {f["file_name"].removesuffix(".json") for f in listed if COMMIT.fullmatch(f["file_name"])}
            archives = {}
            for f in listed:
                if f["fid"] not in baseline or f["file_name"] != baseline[f["fid"]]["file_name"] or QuarkClient.is_directory(f):
                    continue
                if not ARCHIVE.fullmatch(f["file_name"]) or f["fid"] == retained["archive_fid"]:
                    continue
                stem = f["file_name"].removesuffix(".zip")
                if stem == retained["archive_name"].removesuffix(".zip") or stem in live_stems:
                    continue
                # Orphans may belong to another device in the middle of upload.
                # Only reap those older than a day using server time, not names.
                created = int(f.get("created_at", 0))
                created_ms = created * 1000 if 0 < created < 10**12 else created
                stale = 0 < created_ms < int(time.time() * 1000) - 24 * 3600 * 1000
                if stem in old_stems or stale:
                    archives[f["fid"]] = f
            remove(archives)
            return None
        except (ValueError, OSError, KeyError, TypeError):
            return warning

    def upload(self, account, game, expected_revision=None, expected_uid=None):
        with self.lock:
            auth = self.client.account(); self.uid = auth["user_id"]
            if expected_uid is not None and self.uid != expected_uid:
                raise ValueError("夸克账号已变化，请刷新后重试")
            if self.active_account not in (None, account):
                raise ValueError("Steam 账号已变化，已取消同步")
            engine = self.isolated_engine(account)
            if (account, game["game_id"]) in self.running:
                raise ValueError("请先退出游戏再同步")
            with tempfile.TemporaryDirectory() as temp:
                archive = Path(temp) / "save.zip"
                manifest = pack_current(engine, game, archive)
                remote = self.latest(account, game)
                folder = self.directory(account, game, create=True)
                before = self.client.list_files(folder)
                if remote and remote["content_hash"] == manifest["content_hash"]:
                    self.remember(account, game, manifest["content_hash"])
                    warning = self._prune_versions(account, game, folder, before, remote)
                    return {"message": warning or "本地与云端存档内容一致，无需重复上传；云端仅保留最新一份"}
                previous = self.read_state().get(self.key(account, game), {}).get("synced_hash")
                if remote and remote["content_hash"] != previous and expected_revision != remote["revision"]:
                    raise CloudConflict(remote)
                if expected_revision is not None and (remote or {}).get("revision") != expected_revision:
                    raise CloudConflict(remote or {"revision": None, "save_time": None})
                stem = f"version-{int(time.time() * 1000):013d}-{uuid.uuid4().hex}"
                entry = self.client.upload(archive, folder, stem + ".zip")
                # Another device may have published while this one was uploading.
                latest = self.latest(account, game)
                if (latest or {}).get("revision") != (remote or {}).get("revision"):
                    raise CloudConflict(latest or {"revision": None, "save_time": None})
                if self.active_account not in (None, account) or (account, game["game_id"]) in self.running:
                    raise ValueError("游戏已重新启动，未发布新的云端版本")
                engine.guard(game)
                record = {"format": 1, "game_id": int(game["game_id"]), "steam_account": str(account),
                          "archive_name": stem + ".zip", "archive_fid": entry["fid"], "archive_hash": digest(archive),
                          "content_hash": manifest["content_hash"], "save_time": manifest["save_time"], "uploaded_at": int(time.time()*1000)}
                p = Path(temp) / "version.json"
                p.write_text(json.dumps(record), encoding="utf-8")
                published = self.client.upload(p, folder, stem + ".json")
                self.remember(account, game, manifest["content_hash"])
                record["revision"] = published["fid"]
                warning = self._prune_versions(account, game, folder, before, record)
                return {"message": warning or "本地存档已上传，云端仅保留最新一份"}
