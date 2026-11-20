"""只读 Steam appinfo v27/v28/v29；云支持来自 Steam 元数据而非是否存在存档。"""
import io
from pathlib import Path
import struct


def read_exact(stream, size):
    data = stream.read(size)
    if len(data) != size:
        raise ValueError("Steam 元数据不完整")
    return data


def number(stream, code="I"):
    return struct.unpack("<" + code, read_exact(stream, struct.calcsize(code)))[0]


def cstring(stream):
    data = bytearray()
    while len(data) <= 1024 * 1024:
        char = read_exact(stream, 1)
        if char == b"\0":
            return data.decode("utf-8", errors="replace")
        data.extend(char)
    raise ValueError("Steam 元数据字符串过长")


def keyvalues(stream, strings=None, depth=0):
    if depth > 64:
        raise ValueError("Steam 元数据嵌套过深")
    result = {}
    while True:
        kind = number(stream, "B")
        if kind == 8:
            return result
        key = cstring(stream) if strings is None else strings[number(stream)]
        if kind == 0:
            value = keyvalues(stream, strings, depth+1)
        elif kind == 1:
            value = cstring(stream)
        elif kind in (2, 4, 6):
            value = number(stream)
        elif kind == 3:
            value = number(stream, "f")
        elif kind == 7:
            value = number(stream, "Q")
        elif kind == 10:
            value = number(stream, "q")
        else:
            raise ValueError("不支持的 Steam 元数据类型")
        result[key] = value


def cloud_support(steam_root, app_ids):
    wanted = {int(a) for a in app_ids}
    found = {}
    path = Path(steam_root) / "appcache/appinfo.vdf"
    try:
        size = path.stat().st_size
        with path.open("rb") as stream:
            magic = number(stream)
            if magic not in (0x07564427, 0x07564428, 0x07564429):
                return found
            number(stream)  # universe
            strings, boundary = None, size
            if magic == 0x07564429:
                boundary = number(stream, "Q")
                if not 16 <= boundary < size:
                    return found
                start = stream.tell()
                stream.seek(boundary)
                count = number(stream)
                if count > 1000000:
                    return found
                strings = [cstring(stream) for _ in range(count)]
                stream.seek(start)
            while stream.tell() + 4 <= boundary:
                app_id = number(stream)
                if not app_id:
                    break
                length = number(stream)
                end = stream.tell() + length
                if end > boundary or length < (60 if magic >= 0x07564428 else 40):
                    break
                if app_id in wanted:
                    header = 60 if magic >= 0x07564428 else 40
                    if length > 16 * 1024**2:
                        break
                    stream.seek(header, 1)
                    try:
                        app = keyvalues(io.BytesIO(read_exact(stream, length-header)), strings).get("appinfo", {})
                        common = app.get("common", {})
                        categories = common.get("category", {})
                        ufs = app.get("ufs", {})
                        if common.get("name"):
                            found[str(app_id)] = (str(categories.get("category_23", "0")) == "1"
                                                 or int(ufs.get("quota", 0)) > 0
                                                 or bool(ufs.get("savefiles")))
                    except (ValueError, IndexError, TypeError, AttributeError):
                        pass
                stream.seek(end)
    except (OSError, ValueError, struct.error, IndexError):
        pass
    return found
