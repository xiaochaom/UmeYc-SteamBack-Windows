"""Read public GitHub release metadata without sharing Quark credentials."""
from dataclasses import dataclass
import json
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from . import __version__

REPOSITORY_URL = "https://github.com/xiaochaom/UmeYc-SteamBack-Windows"
LATEST_API = "https://api.github.com/repos/xiaochaom/UmeYc-SteamBack-Windows/releases/latest"
MAX_RESPONSE = 1024 * 1024


def version_number(value):
    match = re.fullmatch(r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", value)
    if not match:
        raise ValueError("更新版本格式无法识别")
    return tuple(int(part) for part in match.groups())


@dataclass(frozen=True)
class UpdateResult:
    version: str
    available: bool
    url: str
    package_size: int | None = None


def parse_release(release, current=__version__):
    if not isinstance(release, dict) or release.get("draft") or release.get("prerelease"):
        raise ValueError("未找到可用的正式版本")
    tag = release.get("tag_name")
    if not isinstance(tag, str):
        raise ValueError("更新信息不完整")
    remote_version = version_number(tag)
    available = remote_version > version_number(current)
    package_size = None
    if available:
        assets = release.get("assets", [])
        packages = [a for a in assets if (
            isinstance(a, dict) and a.get("name") == "UmeYc-SteamBack-Windows.zip"
            and a.get("state") == "uploaded" and type(a.get("size")) is int and a["size"] > 0
        )] if isinstance(assets, list) else []
        if len(packages) != 1:
            raise ValueError("新版安装包尚未就绪，请稍后重试")
        package_size = packages[0]["size"]
    # Construct release and asset URLs from the known repository and tag.
    return UpdateResult(".".join(str(p) for p in remote_version), available,
                        REPOSITORY_URL + "/releases/tag/" + tag, package_size)


def check_updates(current=__version__):
    request = Request(LATEST_API, headers={"Accept": "application/vnd.github+json",
                      "User-Agent": "UmeYc-SteamBack-Windows/" + current})
    try:
        with urlopen(request, timeout=8) as response:
            payload = response.read(MAX_RESPONSE + 1)
        if len(payload) > MAX_RESPONSE:
            raise ValueError("更新信息过大，请稍后重试")
        release = json.loads(payload)
    except HTTPError as error:
        if error.code == 404:
            raise ValueError("暂无可用的正式版本") from None
        if error.code in (403, 429):
            raise ValueError("更新检查请求过于频繁，请稍后重试") from None
        raise ValueError("更新服务暂不可用，请稍后重试") from None
    except (URLError, OSError):
        raise ValueError("无法检查更新，请检查网络后重试") from None
    except (ValueError, UnicodeError):
        raise ValueError("更新信息无法读取，请稍后重试") from None
    return parse_release(release, current)
