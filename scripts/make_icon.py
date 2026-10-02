"""Draw the household app's logo and write it as PNG and Windows .ico files.

Pure standard library (no Pillow): shapes are drawn with supersampling and
written with zlib. Run from the project folder:

    .venv\\Scripts\\python.exe scripts\\make_icon.py

Writes homevitals/assets/app_icon.png (256 px) and homevitals/assets/app_icon.ico
(16, 24, 32, 48, 64, 128 and 256 px).
"""
from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "homevitals" / "assets"

TOP = (36, 99, 166)        # blue
BOTTOM = (22, 160, 150)    # teal
HEART = (255, 255, 255)
PULSE = (30, 120, 160)


def _rounded_square(x: float, y: float, radius: float) -> bool:
    # x, y in [0, 1]; a square with rounded corners
    r = radius
    cx = min(max(x, r), 1 - r)
    cy = min(max(y, r), 1 - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def _heart(x: float, y: float) -> bool:
    # Centre the classic implicit heart in the tile.
    hx = (x - 0.5) * 3.15
    hy = (0.47 - y) * 3.15
    a = hx * hx + hy * hy - 1
    return a * a * a - hx * hx * hy * hy * hy <= 0


# The pulse (heartbeat) line, in tile coordinates.
PULSE_POINTS = [(0.22, 0.50), (0.39, 0.50), (0.45, 0.39), (0.51, 0.62), (0.56, 0.45), (0.60, 0.50), (0.78, 0.50)]


def _near_pulse(x: float, y: float, width: float) -> bool:
    for (x1, y1), (x2, y2) in zip(PULSE_POINTS, PULSE_POINTS[1:], strict=False):
        dx, dy = x2 - x1, y2 - y1
        t = max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / (dx * dx + dy * dy)))
        if math.hypot(x - (x1 + t * dx), y - (y1 + t * dy)) <= width / 2:
            return True
    return False


def render(size: int) -> bytes:
    """RGBA pixels for one size, with 4x4 supersampling for smooth edges."""
    samples = 4
    pulse_width = max(0.055, 1.6 / size)          # stays visible at 16 px
    rows = bytearray()
    for py in range(size):
        rows.append(0)                             # PNG filter byte
        for px in range(size):
            r = g = b = a = 0.0
            for sy in range(samples):
                for sx in range(samples):
                    x = (px + (sx + 0.5) / samples) / size
                    y = (py + (sy + 0.5) / samples) / size
                    if not _rounded_square(x, y, 0.22):
                        continue
                    if _heart(x, y):
                        colour = PULSE if _near_pulse(x, y, pulse_width) else HEART
                    else:
                        colour = tuple(int(t + (bt - t) * y) for t, bt in zip(TOP, BOTTOM, strict=True))
                    r += colour[0]
                    g += colour[1]
                    b += colour[2]
                    a += 255
            n = samples * samples
            alpha = a / n
            if alpha:
                cover = a / 255
                rows += bytes((int(r / cover), int(g / cover), int(b / cover), int(alpha)))
            else:
                rows += b"\0\0\0\0"
    return bytes(rows)


def png(size: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(render(size), 9)) + chunk(b"IEND", b""))


def ico(sizes: list[int]) -> bytes:
    images = [png(s) for s in sizes]
    head = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries = b""
    for size, data in zip(sizes, images, strict=True):
        dim = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    return head + entries + b"".join(images)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "app_icon.png").write_bytes(png(256))
    (OUT / "app_icon.ico").write_bytes(ico([16, 24, 32, 48, 64, 128, 256]))
    print(f"Wrote {OUT / 'app_icon.png'} and {OUT / 'app_icon.ico'}")


if __name__ == "__main__":
    main()
