import os
import pytest


@pytest.mark.skipif(os.name != "nt", reason="DPAPI 仅可在真实 Windows 用户会话运行")
def test_dpapi_roundtrip():
    from umeyc_steamback.secrets import crypt
    secret = b'private-quark-cookie'
    protected = crypt(secret)
    assert secret not in protected
    assert crypt(protected, decrypt=True) == secret


@pytest.mark.skipif(os.name != "nt", reason="Windows 目录联接")
def test_junction_escape_is_rejected(tmp_path):
    import subprocess
    from umeyc_steamback.protocol.custom import safe_file
    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    junction = root / "linked"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(outside)], check=True, capture_output=True)
    with pytest.raises(ValueError):
        safe_file(root, "linked/slot.sav")
