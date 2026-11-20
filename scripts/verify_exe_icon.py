"""Check the built PE embeds every image from the cloud-save ICO."""
from pathlib import Path
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
        expected.append(data[offset:offset + size])
    with pefile.PE(str(executable)) as pe:
        images = []
        groups = []
        for entry in pe.DIRECTORY_ENTRY_RESOURCE.entries:
            if entry.id not in (3, 14):
                continue
            for resource in entry.directory.entries:
                for language in resource.directory.entries:
                    info = language.data.struct
                    blob = pe.get_data(info.OffsetToData, info.Size)
                    (images if entry.id == 3 else groups).append(blob)
        assert groups, "EXE has no icon group"
        assert all(blob in images for blob in expected), "EXE does not contain the cloud-save icon"
    print("EXE embeds all cloud-save icon sizes")


if __name__ == "__main__":
    verify(sys.argv[1], sys.argv[2])
