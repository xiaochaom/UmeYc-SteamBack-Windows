"""Generate the Explorer/EXE icon from the existing cloud-save SVG."""
from pathlib import Path
import struct

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QRectF, Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtSvg import QSvgRenderer


def build_icon(source, destination):
    renderer = QSvgRenderer(str(source))
    if not renderer.isValid():
        raise ValueError("Invalid cloud-save SVG")
    images = []
    for size in (16, 24, 32, 48, 64, 128, 256):
        bitmap = QImage(size, size, QImage.Format_ARGB32)
        bitmap.fill(Qt.transparent)
        painter = QPainter(bitmap)
        painter.setRenderHint(QPainter.Antialiasing)
        margin = max(1, size / 16)
        renderer.render(painter, QRectF(margin, margin, size - 2 * margin, size - 2 * margin))
        painter.end()
        if size <= 48:
            # Classic 32-bit DIB entries work in Explorer's small-icon views;
            # PNG entries remain available for high-DPI and large icons.
            pixels = b"".join(bytes((c.blue(), c.green(), c.red(), c.alpha()))
                              for y in reversed(range(size)) for x in range(size)
                              for c in [bitmap.pixelColor(x, y)])
            mask = b"\0" * (((size + 31) // 32) * 4 * size)
            header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, len(pixels), 0, 0, 0, 0)
            images.append((size, header + pixels + mask))
            continue
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.WriteOnly)
        if not bitmap.save(buffer, "PNG"):
            raise ValueError("Cannot render icon")
        images.append((size, bytes(data)))
    offset = 6 + 16 * len(images)
    header = struct.pack("<HHH", 0, 1, len(images))
    for size, payload in images:
        header += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(payload), offset)
        offset += len(payload)
    Path(destination).write_bytes(header + b"".join(data for _, data in images))


if __name__ == "__main__":
    assets = Path(__file__).resolve().parents[1] / "src/umeyc_steamback/assets"
    build_icon(assets / "cloud-save.svg", assets / "cloud-save.ico")
