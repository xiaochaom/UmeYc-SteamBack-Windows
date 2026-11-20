import io
import struct
import pytest
from umeyc_steamback.appinfo import cloud_support
from umeyc_steamback.steam import is_compatibility_tool


def fixture(version, apps=None):
    strings = []
    def string(value):
        return value.encode() + b"\0"
    def key(value):
        if version != 0x29:
            return string(value)
        if value not in strings:
            strings.append(value)
        return struct.pack("<I", strings.index(value))
    def encode(data):
        result = b""
        for name, value in data.items():
            result += (b"\0" if isinstance(value, dict) else b"\1") + key(name)
            result += encode(value) if isinstance(value, dict) else string(str(value))
        return result + b"\x08"
    payload = b""
    if apps is None:
        apps = {42: {"common": {"name": "Cloud", "category": {"category_23": "1"}}},
                43: {"common": {"name": "Local", "category": {"category_2": "1"}}}}
    for aid, app in apps.items():
        body = encode({"appinfo": app})
        header = bytes(60 if version >= 0x28 else 40)
        payload += struct.pack("<II", aid, len(header)+len(body)) + header + body
    payload += bytes(4)
    prefix = struct.pack("<II", 0x07564400 + version, 1)
    if version == 0x29:
        prefix += struct.pack("<Q", 16 + len(payload))
        return prefix + payload + struct.pack("<I", len(strings)) + b"".join(string(s) for s in strings)
    return prefix + payload


@pytest.mark.parametrize("version", [0x27, 0x28, 0x29])
def test_steam_cloud_support_is_from_metadata(tmp_path, version):
    (tmp_path / "appcache").mkdir()
    path = tmp_path / "appcache/appinfo.vdf"
    path.write_bytes(fixture(version))
    assert cloud_support(tmp_path, [42, 43, 44]) == {"42": True, "43": False}
    path.write_bytes(b"invalid")
    assert cloud_support(tmp_path, [42]) == {}


@pytest.mark.parametrize("name", ["Proton 9.0", "Proton - Experimental", "Proton Hotfix", "GE-Proton9-25", "Proton-GE", "Steam Linux Runtime 3.0 (sniper)", "Steam Linux Runtime - Soldier"])
def test_compatibility_tools_are_hidden(name):
    assert is_compatibility_tool(name)


@pytest.mark.parametrize("name", ["Proton Pulse", "明末：渊虚之羽", "SteamWorld Dig", "Portal 2"])
def test_real_games_are_preserved(name):
    assert not is_compatibility_tool(name)


def regression_apps():
    return {
        374320: {"common": {"name": "DARK SOULS III", "category": {"category_2": 1}},
                 "ufs": {"quota": "100000000000", "maxnumfiles": 10000, "hidecloudui": 1,
                         "ignoreexternalfiles": 1, "savefiles": {"0": {
                             "root": "WinAppDataRoaming", "path": "DarkSoulsIII", "pattern": "*.sl2"}}}},
        570940: {"common": {"name": "DARK SOULS REMASTERED", "category": {"category_2": 1}},
                 "ufs": {"quota": 167772160, "savefiles": {"0": {"pattern": "*.sl2"}}}},
        814380: {"common": {"name": "Sekiro", "category": {"category_23": 1}}},
    }


@pytest.mark.parametrize('version', [0x27, 0x28, 0x29])
def test_unused_ufs_configuration_does_not_advertise_steam_cloud(tmp_path, version):
    (tmp_path / 'appcache').mkdir()
    (tmp_path / 'appcache/appinfo.vdf').write_bytes(fixture(version, regression_apps()))
    assert cloud_support(tmp_path, [374320, 570940, 814380, 99]) == {
        '374320': False, '570940': False, '814380': True}
