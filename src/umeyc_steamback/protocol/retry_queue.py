"""Persist upload intent and its original sync baseline across network failures."""
import json
import time
import uuid
from .custom import write_json


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


class UploadQueue:
    def __init__(self, data):
        self.path = data / "pending_uploads.json"
        self.activity = data / "background_activity.json"
        self.pending = read_json(self.path)
        self.previous = None

    @staticmethod
    def key(uid, account, game_id):
        return f"{uid}:{account}:{int(game_id)}"

    def enqueue(self, uid, account, game, base_hash=None, now=None):
        now = time.time() if now is None else now
        key = self.key(uid, account, game["game_id"])
        old = self.pending.get(key)
        # Keep the baseline from the first failed upload. A new exit or retry
        # must never treat the cloud version observed later as permission.
        item = dict(old) if old else {"uid": uid, "account": account, "game": game,
                                     "base_hash": base_hash, "token": uuid.uuid4().hex, "attempts": 0}
        item.update(game=game, due=now + 6, in_game=False)
        self.pending[key] = item
        write_json(self.path, self.pending)
        return dict(item)

    def observe(self, uid, account, games, running, now=None, baselines=None):
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
            elif item["game"]["game_id"] in running and not item.get("in_game"):
                item.update(due=now + 6, in_game=True)
                changed = True
        if old_identity == identity:
            for aid in before - set(running):
                if aid in enabled:
                    self.enqueue(uid, account, enabled[aid], (baselines or {}).get(aid), now)
                    changed = True
        if changed:
            write_json(self.path, self.pending)

    def next(self, uid, account, running, now=None):
        now = time.time() if now is None else now
        return next((dict(item) for item in self.pending.values()
                     if item["uid"] == uid and item["account"] == account
                     and not item.get("blocked") and item["game"]["game_id"] not in running
                     and item["due"] <= now), None)

    def finish(self, item, error=None, now=None, conflict=False):
        key = self.key(item["uid"], item["account"], item["game"]["game_id"])
        now = time.time() if now is None else now
        current = self.pending.get(key)
        if current is not None and current.get("token") == item.get("token"):
            if error is None:
                self.pending.pop(key, None)
            else:
                attempts = item.get("attempts", 0) + 1
                self.pending[key] = {**current, "attempts": attempts, "blocked": conflict,
                                     "due": now + min(60 * 2**min(attempts-1, 4), 900)}
            write_json(self.path, self.pending)
        # Manual synchronization may have cleared the pending task while its
        # worker was finishing. Never recreate that obsolete task on failure.
        records = read_json(self.activity)
        records[key] = {"time": int(now * 1000), "game_name": item["game"]["game_name"],
                        "message": str(error) if error else "游戏退出后云存档已上传",
                        "state": "conflict" if conflict else "error" if error else "done"}
        write_json(self.activity, records)

    def clear(self, uid, account, game_id):
        if self.pending.pop(self.key(uid, account, game_id), None) is not None:
            write_json(self.path, self.pending)
