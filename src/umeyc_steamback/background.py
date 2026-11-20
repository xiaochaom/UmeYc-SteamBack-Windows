"""可恢复的游戏退出上传队列；只监听明确开启自动上传的游戏。"""
import time

from .engine import read_json
from .protocol.custom import write_json


class UploadQueue:
    def __init__(self, data):
        self.path = data / "pending_uploads.json"
        self.activity = data / "background_activity.json"
        self.pending = read_json(self.path)
        self.previous = None

    @staticmethod
    def key(uid, account, game_id):
        return f"{uid}:{account}:{int(game_id)}"

    def observe(self, uid, account, games, running, now=None):
        now = time.time() if now is None else now
        identity = (uid, account)
        old_identity, before = self.previous or (identity, set())
        self.previous = (identity, set(running))
        enabled = {int(g["game_id"]): g for g in games}
        changed = False
        for key, item in list(self.pending.items()):
            if (item["uid"], item["account"]) != identity:
                continue
            if item["game"]["game_id"] not in enabled:
                del self.pending[key]
                changed = True
            elif item["game"]["game_id"] in running:
                # Keep the upload intent across restart/crash, but never upload
                # while the game is running. The next exit resets its delay.
                if not item.get("in_game"):
                    item.update(due=now + 6, attempts=0, in_game=True)
                    changed = True
        if old_identity == identity:
            for aid in before - set(running):
                if aid not in enabled:
                    continue
                key = self.key(uid, account, aid)
                self.pending[key] = {"uid": uid, "account": account, "game": enabled[aid],
                                     "due": now + 6, "attempts": 0}
                changed = True
        if changed:
            write_json(self.path, self.pending)

    def next(self, uid, account, running, now=None):
        now = time.time() if now is None else now
        return next((dict(item) for item in self.pending.values()
                     if item["uid"] == uid and item["account"] == account
                     and item["game"]["game_id"] not in running and item["due"] <= now), None)

    def finish(self, item, error=None, now=None):
        key = self.key(item["uid"], item["account"], item["game"]["game_id"])
        now = time.time() if now is None else now
        if error is None:
            self.pending.pop(key, None)
        else:
            # Network failure, login expiry or conflicts remain visible and
            # retry with bounded frequency, never forced conflict overwrite.
            attempts = item.get("attempts", 0) + 1
            self.pending[key] = {**item, "attempts": attempts,
                                 "due": now + min(60 * 2**min(attempts-1, 4), 900)}
        write_json(self.path, self.pending)
        records = read_json(self.activity)
        records[key] = {"time": int(now * 1000), "game_name": item["game"]["game_name"],
                        "message": str(error) if error else "游戏退出后云存档已上传",
                        "state": "error" if error else "done"}
        write_json(self.activity, records)

    def clear(self, uid, account, game_id):
        if self.pending.pop(self.key(uid, account, game_id), None) is not None:
            write_json(self.path, self.pending)
