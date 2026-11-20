"""共用 Deck 发布协议，Windows 仅替换本地落盘过程。"""
import json
from pathlib import Path
import shutil
import tempfile
import time
import uuid
from dataclasses import replace

from .protocol.cloud import CloudService, CURRENT_BASELINE, digest, unpack_verified
from .protocol.custom import write_json


class WindowsCloud(CloudService):
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
            was_running = (account, aid) in self.running
            self.running.discard((account, aid))
            self.hook_game_id = aid
            try:
                self.isolated_engine(account).guard(game)
                return self._prepare_launch(account, aid)
            finally:
                self.hook_game_id = None
                if was_running:
                    self.running.add((account, aid))

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

    def upload(self, account, game, expected_revision=None, expected_uid=None, base_hash=CURRENT_BASELINE):
        self.isolated_engine(account).guard(game)
        return super().upload(account, game, expected_revision, expected_uid, base_hash=base_hash)

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

    def download(self, account, game, revision, expected_uid):
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
                engine.align_cloud_paths(game, manifest)
                engine.apply(game, manifest, staging, "cloud_sync")
                self.remember(account, game, manifest["content_hash"])
            return {"message": "云端存档已覆盖到游戏存档路径；下载记录及覆盖前备份已保留"}
