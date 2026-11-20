"""夸克网页协议适配；会话仅保存在 Deck 后端，不返回 Cookie 给前端。"""
import base64
from datetime import datetime, timezone
import hashlib
import http.cookiejar
import json
import logging
import os
from pathlib import Path
import ssl
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, quote
from urllib.request import Request, build_opener, HTTPCookieProcessor, HTTPRedirectHandler, HTTPSHandler
import uuid
from xml.sax.saxutils import escape


class QuarkError(ValueError):
    pass


class LoginExpired(QuarkError):
    pass


class _SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        QuarkClient.check_url(newurl)
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and urlsplit(req.full_url).hostname != urlsplit(newurl).hostname:
            for key in ("Authorization", "Cookie"):
                redirected.remove_header(key)
        return redirected


class QuarkClient:
    PAN = "https://pan.quark.cn"
    DRIVE = "https://drive-pc.quark.cn"
    UOP = "https://uop.quark.cn"
    MAX_TRANSFER = 2 * 1024**3
    AUTH_COOKIE_NAMES = ("__pus", "__puus", "__kps", "__uid")
    CA_BUNDLES = ("/etc/ssl/certs/ca-certificates.crt", "/etc/ssl/cert.pem",
                  "/etc/pki/tls/certs/ca-bundle.crt")

    def __init__(self, data_dir):
        self.directory = Path(data_dir) / "quark"
        self.cookie_path = self.directory / "session.cookies"
        self.jar = http.cookiejar.MozillaCookieJar(str(self.cookie_path))
        if self.cookie_path.exists():
            os.chmod(self.cookie_path, 0o600)
            try:
                if os.name == "nt":
                    from ..secrets import crypt
                    for item in json.loads(crypt(self.cookie_path.read_bytes(), decrypt=True)):
                        item["rest"] = item.pop("_rest", {})
                        cookie = http.cookiejar.Cookie(**item)
                        if not cookie.is_expired():
                            self.jar.set_cookie(cookie)
                else:
                    self.jar.load(ignore_discard=True, ignore_expires=False)
            except (OSError, ValueError, TypeError, http.cookiejar.LoadError):
                self.jar.clear()
        context = self._ssl_context()
        https = HTTPSHandler(context=context)
        self.opener = build_opener(_SafeRedirect(), HTTPCookieProcessor(self.jar), https)
        self.transfer_opener = build_opener(_SafeRedirect(), HTTPSHandler(context=self._ssl_context()))
        self.login = None
        self.user_id = next((c.value for c in self.jar if c.name == "__uid"), None)
        identity = self.directory / "identity.json"
        if self.user_id is None and self.cookie_path.exists() and identity.exists():
            try:
                self.user_id = str(json.loads(identity.read_text())["user_id"])
            except (OSError, ValueError, KeyError, TypeError):
                pass

    @classmethod
    def _ssl_context(cls):
        bundle = next((path for path in cls.CA_BUNDLES if Path(path).is_file()), None)
        return ssl.create_default_context(cafile=bundle)

    @staticmethod
    def check_url(url):
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443):
            raise QuarkError("夸克返回了无效的传输地址")
        if not any(host == d or host.endswith("." + d) for d in ("quark.cn", "aliyuncs.com", "uc.cn")):
            raise QuarkError("夸克返回了不受支持的传输域名")

    def _open(self, method, url, body=None, headers=None, authenticated=True):
        self.check_url(url)
        base = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
                "Referer": self.PAN + "/", "Origin": self.PAN}
        base.update(headers or {})
        opener = self.opener if authenticated else self.transfer_opener
        try:
            return opener.open(Request(url, body, base, method=method), timeout=45)
        except HTTPError as error:
            if authenticated and error.code in (401, 403):
                self.user_id = None
                raise LoginExpired("夸克登录已失效，请重新扫码") from None
            raise QuarkError(f"夸克传输失败（HTTP {error.code}），请稍后重试") from None
        except (URLError, OSError, TimeoutError) as error:
            reason = getattr(error, "reason", error)
            logging.getLogger(__name__).warning("夸克连接失败：%s", type(reason).__name__)
            raise QuarkError("无法连接夸克，请检查网络后重试") from None

    def _json(self, method, domain, path, params=None, payload=None, raw=False):
        query = {"pr": "ucpro", "fr": "pc", "uc_param_str": ""}
        query.update(params or {})
        body = None if payload is None else json.dumps(payload).encode()
        with self._open(method, domain + path + "?" + urlencode(query), body,
                        {"Content-Type": "application/json"}) as response:
            try:
                value = json.loads(response.read(4 * 1024**2))
            except (ValueError, UnicodeError):
                raise QuarkError("夸克接口返回格式异常，请稍后重试") from None
        if not isinstance(value, dict):
            raise QuarkError("夸克接口返回格式异常")
        if path == "/account/info":
            data = value.get("data")
            code = value.get("code")
            logging.getLogger(__name__).debug(
                "夸克账号响应诊断：status=%s code=%s success=%s data_keys=%s cookies=%s",
                value.get("status") if isinstance(value.get("status"), (int, str)) else type(value.get("status")).__name__,
                code if isinstance(code, int) or code == "OK" else type(code).__name__, value.get("success") is True,
                sorted(data.keys())[:20] if isinstance(data, dict) else [],
                sorted({cookie.name for cookie in self.jar}),
            )
        successful = value.get("status") in (200, 2000000) or value.get("success") is True or value.get("code") == "OK"
        if not raw and not successful:
            code = value.get("code", value.get("status"))
            if code in (401, 403, 41001, 31001) or "login" in str(value.get("message", "")).lower():
                self.user_id = None
                raise LoginExpired("夸克登录已失效，请重新扫码")
            # Don't include remote payloads: they can contain credentials/URLs.
            raise QuarkError(f"夸克操作未成功（代码 {code}）")
        if self.cookie_path.exists():
            self._save_cookies()
        return value

    def _save_cookies(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        fd, name = tempfile.mkstemp(dir=self.directory)
        os.close(fd)
        try:
            if os.name == "nt":
                from ..secrets import crypt
                Path(name).write_bytes(crypt(json.dumps([vars(c) for c in self.jar if not c.is_expired()]).encode()))
            else:
                self.jar.save(name, ignore_discard=True, ignore_expires=False)
            os.chmod(name, 0o600)
            os.replace(name, self.cookie_path)
        finally:
            Path(name).unlink(missing_ok=True)

    def account(self):
        if not any(c.name in self.AUTH_COOKIE_NAMES for c in self.jar):
            logging.getLogger(__name__).warning(
                "夸克账号校验没有认证 Cookie；当前 Cookie 名称：%s",
                sorted({cookie.name for cookie in self.jar}),
            )
            self.user_id = None
            raise LoginExpired("请先登录夸克")
        data = self._json("GET", self.PAN, "/account/info").get("data", {})
        uid = data.get("uid") or data.get("user_id") or next(
            (cookie.value for cookie in self.jar if cookie.name == "__uid"), None)
        if not uid:
            logging.getLogger(__name__).warning(
                "夸克账号响应中没有用户 ID；data_keys=%s cookies=%s",
                sorted(data.keys())[:20], sorted({cookie.name for cookie in self.jar}),
            )
            self.user_id = None
            raise LoginExpired("夸克登录已失效，请重新扫码")
        self.user_id = str(uid)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, name = tempfile.mkstemp(dir=self.directory)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump({"user_id": self.user_id}, stream)
            os.replace(name, self.directory / "identity.json")
        finally:
            Path(name).unlink(missing_ok=True)
        nickname = data.get("nickname")
        nickname = nickname.strip() if isinstance(nickname, str) else None
        return {"logged_in": True, "user_id": self.user_id, "nickname": nickname or None}

    def begin_login(self):
        result = self._json("GET", self.UOP, "/cas/ajax/getTokenForQrcodeLogin",
                            {"client_id": "532", "v": "1.2", "request_id": str(uuid.uuid4())})
        token = result["data"]["members"]["token"]
        session = uuid.uuid4().hex
        self.login = {"id": session, "token": token, "expires": time.monotonic() + 300}
        url = "https://su.quark.cn/4_eMHBJ?" + urlencode({"token": token, "client_id": "532", "ssb": "weblogin",
              "uc_param_str": "", "uc_biz_str": "S:custom|OPT:SAREA@0|OPT:IMMERSIVE@1|OPT:BACK_BTN_STYLE@0"})
        return {"session_id": session, "url": url, "expires_in": 300}

    def poll_login(self, session_id):
        login = self.login
        if not login or login["id"] != session_id or time.monotonic() >= login["expires"]:
            return {"state": "expired"}
        result = self._json("GET", self.UOP, "/cas/ajax/getServiceTicketByQrcodeToken",
                            {"client_id": "532", "v": "1.2", "request_id": str(uuid.uuid4()), "token": login["token"]}, raw=True)
        status = result.get("status")
        ticket = result.get("data", {}).get("members", {}).get("service_ticket")
        if ticket or status not in (50004001, 2000000):
            logging.getLogger(__name__).warning(
                "夸克扫码轮询诊断：status=%s ticket_present=%s member_keys=%s",
                status if isinstance(status, (int, str)) else type(status).__name__, bool(ticket),
                sorted(result.get("data", {}).get("members", {}))[:20]
                if isinstance(result.get("data"), dict) and isinstance(result.get("data", {}).get("members"), dict) else [],
            )
        if status == 2000000 and ticket:
            self._json("GET", self.PAN, "/account/info", {"st": ticket, "lw": "scan"})
            account = self.account()
            self._save_cookies()
            self.login = None
            return {"state": "logged_in", **account}
        if status == 50004001 or status == 2000000:
            return {"state": "waiting"}
        if status not in (50004002, 50004003, 50004004):
            raise QuarkError("夸克扫码状态异常，请稍后重试")
        self.login = None
        return {"state": "expired" if status == 50004002 else "cancelled"}

    def cancel_login(self, session_id):
        if self.login and self.login["id"] == session_id:
            self.login = None

    def logout(self):
        self.login = None
        self.user_id = None
        self.jar.clear()
        self.cookie_path.unlink(missing_ok=True)
        (self.directory / "identity.json").unlink(missing_ok=True)
        return {"logged_in": False, "user_id": None}

    def list_files(self, folder):
        files = []
        for page in range(1, 1001):
            result = self._json("GET", self.DRIVE, "/1/clouddrive/file/sort",
                                {"pdir_fid": folder, "_page": page, "_size": 100, "_sort": "file_name:asc", "_fetch_total": 1})
            batch = result["data"]["list"]
            files.extend(batch)
            if len(batch) < 100:
                return files
        raise QuarkError("云端目录文件过多，请整理后重试")

    @staticmethod
    def is_directory(entry):
        return entry.get("dir") is True or entry.get("file_type") == 0

    def delete_files(self, file_ids):
        """仅删除调用方明确指定的文件，不清空目录或账号回收站。"""
        if not file_ids or len(file_ids) > 100:
            raise ValueError("云端删除文件数量无效")
        return self._json("POST", self.DRIVE, "/1/clouddrive/file/delete", payload={
            "action_type": 2, "filelist": list(file_ids), "exclude_fids": []})

    def folder(self, parent, name, create=False):
        matches = [f for f in self.list_files(parent) if f["file_name"] == name]
        if matches:
            if len(matches) != 1 or not self.is_directory(matches[0]):
                raise QuarkError(f"云端目录名称冲突：{name}")
            return matches[0]["fid"]
        if not create:
            return None
        value = self._json("POST", self.DRIVE, "/1/clouddrive/file", payload={
            "pdir_fid": parent, "file_name": name, "dir_path": "", "dir_init_lock": False})
        return value["data"]["fid"]

    def download(self, fid, destination, limit=None):
        limit = min(limit or self.MAX_TRANSFER, self.MAX_TRANSFER)
        value = self._json("POST", self.DRIVE, "/1/clouddrive/file/download", payload={"fids": [fid]})
        url = value["data"][0]["download_url"]
        # CookieJar applies domain restrictions, including across redirects.
        with self._open("GET", url) as response, open(destination, "wb") as output:
            total = 0
            while True:
                block = response.read(1024**2)
                if not block:
                    break
                total += len(block)
                if total > limit:
                    raise QuarkError("云端存档超过支持的大小")
                output.write(block)

    def upload(self, source, folder, name):
        source = Path(source)
        size = source.stat().st_size
        if not 0 < size <= self.MAX_TRANSFER:
            raise QuarkError("上传文件为空或超过 2 GiB")
        md5, sha1 = hashlib.md5(), hashlib.sha1()
        with source.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024**2), b""):
                md5.update(chunk); sha1.update(chunk)
        mime = "application/octet-stream"
        now = int(time.time() * 1000)
        pre = self._json("POST", self.DRIVE, "/1/clouddrive/file/upload/pre", payload={
            "ccp_hash_update": True, "dir_name": "", "file_name": name, "format_type": mime,
            "l_created_at": now, "l_updated_at": now, "pdir_fid": folder, "size": size})
        info = pre["data"]
        result = self._json("POST", self.DRIVE, "/1/clouddrive/file/update/hash", payload={
            "task_id": info["task_id"], "md5": md5.hexdigest(), "sha1": sha1.hexdigest()})
        if not result.get("data", {}).get("finish"):
            self._multipart(source, info, pre.get("metadata", {}), mime)
        # Do not publish a cloud version until Quark lists the complete file.
        for attempt in range(6):
            matches = [f for f in self.list_files(folder) if f["file_name"] == name and int(f.get("size", -1)) == size]
            if len(matches) == 1:
                return matches[0]
            time.sleep(1)
        raise QuarkError("夸克尚未确认文件上传完成，请刷新后重试")

    def _multipart(self, source, info, metadata, mime):
        bucket, key = info["bucket"], info["obj_key"]
        host = urlsplit("https://" + info["upload_url"].removeprefix("https://").removeprefix("http://")).hostname
        if not host or not bucket.replace("-", "").isalnum():
            raise QuarkError("夸克上传地址异常")
        host = host if host.startswith(bucket + ".") else bucket + "." + host
        base = "https://" + host + "/" + quote(key, safe="/")
        self.check_url(base)
        part_size = max(1024**2, min(int(metadata.get("part_size", 4194304)), 16 * 1024**2))
        parts = []

        def signed_send(method, body, query, content_type, extra=None):
            date = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
            headers = {"x-oss-date": date, "x-oss-user-agent": "aliyun-sdk-js/6.6.1", **(extra or {})}
            canonical = "".join(f"{k}:{headers[k]}\n" for k in sorted(headers) if k.startswith("x-oss-"))
            resource = f"/{bucket}/{key}?{query}"
            auth_meta = f"{method}\n{headers.get('Content-MD5', '')}\n{content_type}\n{date}\n{canonical}{resource}"
            auth = self._json("POST", self.DRIVE, "/1/clouddrive/file/upload/auth", payload={
                "auth_info": info["auth_info"], "auth_meta": auth_meta, "task_id": info["task_id"]})
            headers.update({"Authorization": auth["data"]["auth_key"], "Content-Type": content_type})
            with self._open(method, base + "?" + query, body, headers, authenticated=False) as response:
                result = (response.headers.get("ETag"), response.read(1024**2))
            return result

        with source.open("rb") as stream:
            number = 0
            while chunk := stream.read(part_size):
                number += 1
                query = urlencode({"partNumber": number, "uploadId": info["upload_id"]})
                etag, _ = signed_send("PUT", chunk, query, mime)
                if not etag:
                    raise QuarkError("夸克未返回上传分片校验值")
                parts.append((number, etag.strip('"')))
        xml = ('<?xml version="1.0" encoding="UTF-8"?>\n<CompleteMultipartUpload>' +
               "".join(f"<Part><PartNumber>{n}</PartNumber><ETag>{escape(e)}</ETag></Part>" for n, e in parts) +
               "</CompleteMultipartUpload>").encode()
        extra = {"Content-MD5": base64.b64encode(hashlib.md5(xml).digest()).decode(),
                 "x-oss-callback": base64.b64encode(json.dumps(info["callback"]).encode()).decode()}
        _, response = signed_send("POST", xml, urlencode({"uploadId": info["upload_id"]}), "application/xml", extra)
        if b"<Error>" in response:
            raise QuarkError("夸克上传合并失败，旧云存档已保留")
