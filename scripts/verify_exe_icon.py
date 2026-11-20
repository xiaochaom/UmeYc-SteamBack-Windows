"""Check the built PE embeds every image from the cloud-save ICO."""
from pathlib import Path
import os
import struct
import sys

import pefile


def verify(executable, icon):
    data = Path(icon).read_bytes()
    reserved, kind, count = struct.unpack_from("<HHH", data)
    assert reserved == 0 and kind == 1 and count >= 7
    expected = []
    for index in range(count):
        *_, size, offset = struct.unpack_from("<BBBBHHII", data, 6 + 16 * index)
        expected.append((data[6 + 16 * index:6 + 16 * index + 12], data[offset:offset + size]))
    with pefile.PE(str(executable)) as pe:
        images = {}
        groups = []
        for entry in pe.DIRECTORY_ENTRY_RESOURCE.entries:
            if entry.id not in (3, 14):
                continue
            for resource in entry.directory.entries:
                for language in resource.directory.entries:
                    info = language.data.struct
                    blob = pe.get_data(info.OffsetToData, info.Size)
                    if entry.id == 3:
                        images[resource.id, language.id] = blob
                    else:
                        groups.append((language.id, blob))
        assert len(groups) == 1, "EXE must have exactly one cloud-save icon group"
        language, group = groups[0]
        assert struct.unpack_from("<HHH", group) == (0, 1, count)
        for index, (entry, image) in enumerate(expected):
            offset = 6 + 14 * index
            assert group[offset:offset + 12] == entry, "Default icon group has incorrect size entries"
            resource_id, = struct.unpack_from("<H", group, offset + 12)
            assert images[resource_id, language] == image, "Default icon group references a different image"
    if os.name == "nt":
        # Check what Windows actually extracts as the default icon, rather than
        # only proving that some cloud icon resources exist inside the EXE.
        assert shell_icons(executable) == shell_icons(icon), "Windows extracts a different default icon"
    print("EXE embeds all cloud-save icon sizes")


def shell_icons(path):
    import ctypes
    from ctypes import wintypes

    class ICONINFO(ctypes.Structure):
        _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD),
                    ("yHotspot", wintypes.DWORD), ("hbmMask", wintypes.HBITMAP),
                    ("hbmColor", wintypes.HBITMAP)]

    class BITMAP(ctypes.Structure):
        _fields_ = [("bmType", wintypes.LONG), ("bmWidth", wintypes.LONG),
                    ("bmHeight", wintypes.LONG), ("bmWidthBytes", wintypes.LONG),
                    ("bmPlanes", wintypes.WORD), ("bmBitsPixel", wintypes.WORD),
                    ("bmBits", ctypes.c_void_p)]

    extract = ctypes.windll.shell32.ExtractIconExW
    extract.argtypes = [wintypes.LPCWSTR, ctypes.c_int, ctypes.POINTER(wintypes.HICON),
                        ctypes.POINTER(wintypes.HICON), wintypes.UINT]
    extract.restype = wintypes.UINT
    user, gdi = ctypes.windll.user32, ctypes.windll.gdi32
    user.GetIconInfo.argtypes = [wintypes.HICON, ctypes.POINTER(ICONINFO)]
    user.DestroyIcon.argtypes = [wintypes.HICON]
    gdi.GetObjectW.argtypes = [wintypes.HGDIOBJ, ctypes.c_int, ctypes.c_void_p]
    gdi.GetBitmapBits.argtypes = [wintypes.HBITMAP, wintypes.LONG, ctypes.c_void_p]
    gdi.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    large, small = wintypes.HICON(), wintypes.HICON()
    result = []
    try:
        assert extract(str(Path(path).resolve()), 0, ctypes.byref(large), ctypes.byref(small), 1) == 1
        for handle in (large, small):
            info = ICONINFO()
            assert user.GetIconInfo(handle, ctypes.byref(info))
            try:
                bitmap = BITMAP()
                assert gdi.GetObjectW(info.hbmColor, ctypes.sizeof(bitmap), ctypes.byref(bitmap))
                size = bitmap.bmWidthBytes * abs(bitmap.bmHeight)
                pixels = ctypes.create_string_buffer(size)
                assert gdi.GetBitmapBits(info.hbmColor, size, pixels) == size
                result.append((bitmap.bmWidth, bitmap.bmHeight, bitmap.bmBitsPixel, pixels.raw))
            finally:
                for image in (info.hbmColor, info.hbmMask):
                    if image:
                        gdi.DeleteObject(image)
    finally:
        for handle in (large, small):
            if handle:
                user.DestroyIcon(handle)
    return result


if __name__ == "__main__":
    verify(sys.argv[1], sys.argv[2])
