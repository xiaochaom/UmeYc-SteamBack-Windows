"""共用 Deck 发布协议，Windows 仅替换本地落盘过程。"""
import json
from pathlib import Path
import shutil
import tempfile
import time
import uuid
from dataclasses import replace

from .protocol.cloud import CloudService, CURRENT_BASELINE, digest, unpack_verified, pack_current
from .protocol.custom import write_json


class WindowsCloud(CloudService):
    def manual_snapshot(self, account, game):
        """Read fresh comparison data; never authorize from cached UI times."""
        with self.lock:
            engine = self.isolated_engine(account)
            engine.guard(game)
            uid = self.client.account()["user_id"]
            if self.active_account not in (None, account):
                raise ValueError("Steam 账号已变化，请重新同步")
            self.uid = uid
            remote = self.latest(account, game)
            return {"uid": uid, "remote": remote, "local_time": engine.save_time(game),
                    "local_hash": self._local_hash(engine, game), "settings": engine.settings(game)}

    def _local_hash(self, engine, game):
        plan = engine._custom_plan(game)
        if not any(plan["custom_files"].values()):
            return None
        with tempfile.TemporaryDirectory(dir=engine.data) as temp:
            return pack_current(engine, game, Path(temp) / "local.zip")["content_hash"]

    def manual_transfer(self, account, game, direction, snapshot):
        with self.lock:
            current = self.manual_snapshot(account, game)
            if (current["uid"] != snapshot["uid"] or current["local_hash"] != snapshot["local_hash"]
                    or current["local_time"] != snapshot["local_time"] or current["settings"] != snapshot["settings"]
                    or (current["remote"] or {}).get("revision") != (snapshot["remote"] or {}).get("revision")):
                raise ValueError("确认期间存档或账号已变化，请重新点击同步并确认")
            remote = current["remote"]
            if direction == "upload":
                return self.upload(account, game, (remote or {}).get("revision"), current["uid"], confirmed_source=current)
            if direction != "download" or not remote:
                raise ValueError("云端暂无可下载的存档")
            return self.download(account, game, remote["revision"], current["uid"],
                                 local_snapshot=current)

    def isolated_engine(self, account):
        engine = super().isolated_engine(account)
        if getattr(self, "hook_game_id", None) is not None:
            engine.config = replace(engine.config, prelaunch_game_id=self.hook_game_id)
        return engine

    def prepare_steam_launch(self, account, game):
        from .desktop import check_steam_account
        with self.lock:
            check_steam_account(account)
            aid = game["game_id"]
            self.hook_game_id = aid
            try:
                self.isolated_engine(account).guard(game)
                self.running.discard((account, aid))
                return self._prepare_launch(account, aid)
            finally:
                self.hook_game_id = None

    def upload_after_exit(self, account, game, uid, wait=None, base_hash=CURRENT_BASELINE):
        from .desktop import check_steam_account
        from .protocol.cloud import source_plan
        from .protocol.custom import safe_file
        wait = wait or time.sleep
        previous = None
        for _ in range(15):
            check_steam_account(account)
            engine = self.isolated_engine(account)
            engine.guard(game)
            info, files, _ = source_plan(engine, game)
            current = [(root, f, safe_file(root, f).stat().st_size, safe_file(root, f).stat().st_mtime_ns)
                       for root, suffix in info["save_games_roots"].items() for f in files[suffix]]
            if current and current == previous:
                return self.upload(account, game, expected_uid=uid, base_hash=base_hash)
            previous = current
            wait(2)
        raise ValueError("存档仍在变化，已延后自动上传")

    def _download_for_launch(self, account, game, revision, uid):
        self.download(account, game, revision, uid)

    def prepare_game_launch(self, account, game):
        self.isolated_engine(account).guard(game)
        return self._prepare_launch(account, game["game_id"])

    def upload(self, account, game, expected_revision=None, expected_uid=None, base_hash=CURRENT_BASELINE, confirmed_source=None):
        with self.lock:
            self.isolated_engine(account).guard(game)
            # The process/Steam check just succeeded. An earlier monitor tick
            # must not override it with a stale running flag. The base upload
            # still checks again before publishing the new cloud version.
            self.running.discard((account, game["game_id"]))
            result = super().upload(account, game, expected_revision, expected_uid,
                                    base_hash=base_hash, confirmed_source=confirmed_source)
            return {**result, "uid": self.uid, "account": account, "game_id": int(game["game_id"])}

    def inspect(self, account, game, expected_revision=None):
        with self.lock:
            self.uid = self.client.account()["user_id"]
            remote = self.latest(account, game)
            if not remote:
                raise ValueError("云端暂无存档，请先从另一台设备上传")
            if expected_revision and remote["revision"] != expected_revision:
                raise ValueError("云端版本已变化，请刷新后重试")
            with tempfile.TemporaryDirectory(dir=self.engine.config.app_data_dir) as temp:
                archive = Path(temp) / "save.zip"
                self.client.download(remote["archive_fid"], archive)
                if digest(archive) != remote["archive_hash"]:
                    raise ValueError("云存档压缩包校验失败")
                manifest = unpack_verified(archive, Path(temp) / "files", game, account, remote["content_hash"])
            return {"manifest": manifest, "remote": remote, "uid": self.uid}

    def download_current(self, account, game):
        with self.lock:
            self.isolated_engine(account).guard(game)
            uid = self.client.account()["user_id"]
            if self.uid is not None and uid != self.uid:
                raise ValueError("夸克账号已变化，请刷新后重试")
            self.uid = uid
            if self.active_account not in (None, account):
                raise ValueError("Steam 账号已变化，请刷新后重试")
            remote = self.latest(account, game)
            if not remote:
                raise ValueError("云端暂无存档，请先从另一台设备上传")
            return self.download(account, game, remote["revision"], uid)

    def download(self, account, game, revision, expected_uid, local_snapshot=None):
        with self.lock:
            self.uid = self.client.account()["user_id"]
            if self.uid != expected_uid or self.active_account not in (None, account):
                raise ValueError("账号已变化，请刷新后重试")
            engine = self.isolated_engine(account)
            engine.guard(game)
            remote = self.latest(account, game)
            if not remote or remote["revision"] != revision:
                raise ValueError("云端版本已变化，请刷新后重新确认")
            with tempfile.TemporaryDirectory(dir=engine.data) as temp:
                archive = Path(temp) / "save.zip"
                self.client.download(remote["archive_fid"], archive)
                if digest(archive) != remote["archive_hash"]:
                    raise ValueError("云存档校验失败，未覆盖游戏存档")
                staging = Path(temp) / "files"
                manifest = unpack_verified(archive, staging, game, account, remote["content_hash"])
                # Retain the download even if applying fails. The extracted
                # temporary directory is never configured as a live save path.
                folder = engine.backup_dir(game)
                folder.mkdir(parents=True, exist_ok=True)
                name = f"{int(time.time()*1000)}-{uuid.uuid4().hex}"
                copy = folder / f"{name}.zip"
                try:
                    shutil.copy2(archive, copy)
                    write_json(folder / f"{name}.json", {"id": name, "name": "", "source": "cloud_download",
                               "time": int(time.time()*1000), "save_time": manifest["save_time"],
                               "content_hash": manifest["content_hash"], "archive_hash": remote["archive_hash"]})
                except BaseException:
                    copy.unlink(missing_ok=True)
                    raise
                # Do not apply an obsolete download if another machine
                # published while transferring, or the game restarted.
                current = self.latest(account, game)
                if not current or current["revision"] != revision:
                    raise ValueError("云端版本已变化，下载已保留但未覆盖游戏存档，请重新同步")
                if self.client.account()["user_id"] != expected_uid or self.active_account not in (None, account):
                    raise ValueError("账号已变化，未覆盖游戏存档")
                engine.guard(game)
                if local_snapshot is not None and (
                        self._local_hash(engine, game) != local_snapshot["local_hash"]
                        or engine.save_time(game) != local_snapshot["local_time"]
                        or engine.settings(game) != local_snapshot["settings"]):
                    raise ValueError("下载期间本地存档已变化，未覆盖，请重新同步并确认")
                engine.align_cloud_paths(game, manifest)
                engine.apply(game, manifest, staging, "cloud_sync")
                self.remember(account, game, manifest["content_hash"])
            return {"message": "云端存档已覆盖到游戏存档路径；下载记录及覆盖前备份已保留",
                    "remote": remote, "uid": self.uid, "account": account, "game_id": int(game["game_id"])}
