import copy
from pathlib import Path
from umeyc_steamback.protocol.quark import QuarkError, LoginExpired

class MemoryQuark:
    def __init__(self):
        self.user_id = "quark-user"
        self.items = {}
        self.payloads = {}
        self.fail_commit = False
        self.expired = False
        self.sequence = 0
        self.deleted = []
        self.fail_delete = False

    def next_id(self):
        self.sequence += 1
        return str(self.sequence)

    def account(self):
        if self.expired:
            raise LoginExpired("夸克登录已失效，请重新扫码")
        return {"logged_in": True, "user_id": self.user_id}

    def list_files(self, folder):
        return [copy.deepcopy(v) for v in self.items.values() if v["parent"] == folder]

    def folder(self, parent, name, create=False):
        match = next((f for f in self.list_files(parent) if f["file_name"] == name), None)
        if match:
            return match["fid"]
        if create:
            fid = self.next_id()
            self.items[fid] = {"fid": fid, "parent": parent, "file_name": name, "dir": True}
            return fid
        return None

    def upload(self, source, folder, name):
        if self.fail_commit and name.endswith('.json'):
            raise QuarkError("模拟网络失败")
        fid = self.next_id()
        data = Path(source).read_bytes()
        self.items[fid] = {"fid": fid, "parent": folder, "file_name": name, "size": len(data), "created_at": self.sequence}
        self.payloads[fid] = data
        return self.items[fid]

    def delete_files(self, file_ids):
        if self.fail_delete:
            raise QuarkError("模拟删除失败")
        self.deleted.extend(file_ids)
        for fid in file_ids:
            self.items.pop(fid, None)
            self.payloads.pop(fid, None)

    def download(self, fid, destination, limit=None):
        data = self.payloads[fid]
        if limit and len(data) > limit:
            raise ValueError("超过限制")
        Path(destination).write_bytes(data)
